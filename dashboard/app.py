from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import settings
from storage.schema import JARVIS_ROOT

ROOT = Path(__file__).resolve().parents[1]
LOGO_PATH = ROOT / "dashboard" / "assets" / "marvis-mark.svg"
DOCS_PATH = ROOT / "docs" / "index.html"
LOG_PATH = ROOT / "logs" / "jarvis.log"
DB_PATH = ROOT / "data" / "jarvis_memory.db"
TOKEN_PATH = ROOT / "token.json"
GMAIL_STATE_PATH = ROOT / "data" / "gmail_state.txt"
GMAIL_ACTIVITY_PATH = ROOT / "data" / "gmail_activity.jsonl"

logger = logging.getLogger(__name__)
_DRIVE_CACHE_TTL_SECONDS = 60.0
_drive_snapshot_cache: dict[str, Any] = {"fetched_at": 0.0, "payload": None}

_PROCESSING_RE = re.compile(
    r"^(?P<ts>\S+ \S+) \[INFO\] __main__: Processing email: from=(?P<sender>.+?) subject=(?P<subject>.+?) attachments=(?P<attachments>\d+)$"
)
_SKIPPED_RE = re.compile(
    r"^(?P<ts>\S+ \S+) \[INFO\] __main__: Skipping email \(not worth filing\): (?P<subject>.+?) — (?P<reason>.+)$"
)
_NO_ATTACHMENTS_RE = re.compile(
    r"^(?P<ts>\S+ \S+) \[INFO\] __main__: Email marked worth filing but has no attachments: (?P<subject>.+)$"
)
_FILED_RE = re.compile(
    r"^(?P<ts>\S+ \S+) \[INFO\] __main__: Filed attachment '(?P<filename>.+?)' -> (?P<top_level>.+?)/(?P<sub_folder>.+?) \(Drive ID: (?P<drive_id>.+?)\)$"
)


@dataclass
class ConnectivityItem:
    name: str
    status: str
    detail: str


@dataclass
class DashboardSnapshot:
    generated_at: str
    app_status: str
    memory_count: str
    task_count: str
    financial_count: str
    connectivity: list[ConnectivityItem]
    recent_email_activity: list["EmailActivityItem"]
    recent_log_lines: list[str]
    processed_summary: Counter
    last_gmail_state: str
    active_memories: list["MemoryItem"]
    drive_status: str
    drive_detail: str
    drive_files: list["DriveFileItem"]


@dataclass
class EmailActivityItem:
    processed_at: str
    outcome: str
    subject: str
    sender: str
    reason: str
    attachment_count: str


@dataclass
class MemoryItem:
    topic: str
    summary: str
    category: str
    source: str
    confidence: str
    created_at: str
    updated_at: str
    document_ref: str


@dataclass
class DriveFileItem:
    path: str
    name: str
    mime_type: str
    modified_time: str
    web_view_link: str


def _read_lines(path: Path, limit: int = 200) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as exc:
        return [f"[dashboard] failed to read {path.name}: {exc}"]
    return [line.rstrip("\n") for line in lines[-limit:]]


def _app_status(lines: list[str]) -> str:
    for line in reversed(lines):
        if "Application is stopping" in line:
            return "stopping"
        if "Application started" in line:
            return "running"
        if "Starting Marvis" in line or "Starting Jarvis" in line:
            return "starting"
    return "unknown"


def _db_counts(path: Path) -> tuple[str, str, str]:
    if not path.exists():
        return "unavailable", "unavailable", "unavailable"

    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        memory_count = cur.execute("SELECT COUNT(*) FROM memories WHERE active=1").fetchone()[0]
        task_count = cur.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        financial_count = cur.execute("SELECT COUNT(*) FROM financial_records").fetchone()[0]
        return str(memory_count), str(task_count), str(financial_count)
    except Exception as exc:
        logger.warning("Dashboard DB read failed: %s", exc)
        return "unavailable", "unavailable", "unavailable"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_active_memories(path: Path) -> tuple[list[MemoryItem], str]:
    if not path.exists():
        return [], "database not found"

    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT topic, summary, category, source, confidence, document_ref, created_at, updated_at
            FROM memories
            WHERE active=1
            ORDER BY updated_at DESC, created_at DESC, topic
            """
        ).fetchall()
        memories = [
            MemoryItem(
                topic=str(row["topic"] or ""),
                summary=str(row["summary"] or ""),
                category=str(row["category"] or ""),
                source=str(row["source"] or ""),
                confidence=str(row["confidence"] or ""),
                created_at=str(row["created_at"] or ""),
                updated_at=str(row["updated_at"] or ""),
                document_ref=str(row["document_ref"] or ""),
            )
            for row in rows
        ]
        return memories, "live sqlite data"
    except Exception as exc:
        logger.warning("Dashboard memory read failed: %s", exc)
        return [], f"unavailable: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _connectivity_summary(log_text: str) -> list[ConnectivityItem]:
    checks = [
        (
            "Gmail",
            "Gmail watcher started" in log_text,
            "watcher thread active" if "Gmail watcher started" in log_text else "not seen in logs",
        ),
        (
            "Drive",
            "Drive client initialised" in log_text,
            "Drive initialized" if "Drive client initialised" in log_text else "not seen in logs",
        ),
        (
            "Calendar",
            "Calendar client initialised" in log_text,
            "Calendar initialized" if "Calendar client initialised" in log_text else "not seen in logs",
        ),
        (
            "Telegram",
            "Telegram bot starting" in log_text and "Application started" in log_text,
            "polling" if "Application started" in log_text else "not seen in logs",
        ),
    ]
    items = []
    for name, ok, detail in checks:
        items.append(
            ConnectivityItem(
                name=name,
                status="connected" if ok else "unknown",
                detail=detail,
            )
        )
    if TOKEN_PATH.exists():
        items.append(ConnectivityItem(name="Google Token", status="present", detail="token.json exists"))
    else:
        items.append(ConnectivityItem(name="Google Token", status="missing", detail="token.json not found"))
    return items


def _gmail_activity(limit: int = 24) -> tuple[list[EmailActivityItem], Counter]:
    if not GMAIL_ACTIVITY_PATH.exists():
        return _gmail_activity_from_logs(limit=limit)

    events: list[EmailActivityItem] = []
    summary = Counter()
    try:
        raw_lines = _read_lines(GMAIL_ACTIVITY_PATH, limit=limit)
    except Exception as exc:
        logger.warning("Dashboard Gmail activity read failed: %s", exc)
        return [], Counter()

    for raw in raw_lines:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        outcome = str(payload.get("outcome", "unknown"))
        summary[outcome] += 1
        events.append(
            EmailActivityItem(
                processed_at=str(payload.get("processed_at", "")),
                outcome=outcome,
                subject=str(payload.get("subject", "")),
                sender=str(payload.get("from", "")),
                reason=str(payload.get("reason", "")),
                attachment_count=str(payload.get("attachment_count", "")),
            )
        )

    return events[-limit:], summary


def _gmail_activity_from_logs(limit: int = 24) -> tuple[list[EmailActivityItem], Counter]:
    lines = _read_lines(LOG_PATH, limit=500)
    events: list[EmailActivityItem] = []
    summary = Counter()
    current: EmailActivityItem | None = None

    for line in lines:
        match = _PROCESSING_RE.match(line)
        if match:
            if current is not None:
                summary[current.outcome] += 1
                events.append(current)
            current = EmailActivityItem(
                processed_at=match.group("ts"),
                outcome="processing",
                subject=match.group("subject"),
                sender=match.group("sender"),
                reason="",
                attachment_count=match.group("attachments"),
            )
            continue

        if current is None:
            continue

        match = _SKIPPED_RE.match(line)
        if match and match.group("subject") == current.subject:
            current.outcome = "skipped"
            current.reason = match.group("reason")
            summary[current.outcome] += 1
            events.append(current)
            current = None
            continue

        match = _NO_ATTACHMENTS_RE.match(line)
        if match and match.group("subject") == current.subject:
            current.outcome = "no_attachments"
            current.reason = "worth filing but email had no attachments"
            summary[current.outcome] += 1
            events.append(current)
            current = None
            continue

        match = _FILED_RE.match(line)
        if match:
            current.outcome = "filed"
            current.reason = f"stored in {match.group('top_level')}/{match.group('sub_folder')}"

    if current is not None:
        summary[current.outcome] += 1
        events.append(current)

    return events[-limit:], summary


def _gmail_state() -> str:
    if not GMAIL_STATE_PATH.exists():
        return "no state file yet"
    try:
        content = GMAIL_STATE_PATH.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as exc:
        return f"unable to read: {exc}"
    return content or "empty state file"


def _build_drive_service():
    credentials_path = Path(settings.GOOGLE_CREDENTIALS_PATH)
    token_path = Path(settings.GOOGLE_TOKEN_PATH)
    if not credentials_path.exists() or not token_path.exists():
        return None, "Google credentials/token not available"

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except Exception as exc:
        return None, f"Google client libraries unavailable: {exc}"

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), settings.GOOGLE_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            return None, "Google Drive credentials are invalid"
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return service, "live drive data"
    except Exception as exc:
        logger.warning("Dashboard drive auth failed: %s", exc)
        return None, f"unavailable: {exc}"


def _list_jarvis_drive_files(service, parent_id: str, path_prefix: str, visited: set[str], limit: int = 200) -> list[DriveFileItem]:
    if parent_id in visited:
        return []
    visited.add(parent_id)

    items: list[DriveFileItem] = []
    page_token = None
    while True:
        result = service.files().list(
            q=f"'{parent_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, parents, webViewLink, modifiedTime)",
            pageSize=100,
            pageToken=page_token,
            orderBy="folder,name",
        ).execute()
        for file in result.get("files", []):
            mime_type = str(file.get("mimeType", ""))
            name = str(file.get("name", ""))
            file_id = str(file.get("id", ""))
            if mime_type == "application/vnd.google-apps.folder":
                items.extend(
                    _list_jarvis_drive_files(
                        service,
                        file_id,
                        f"{path_prefix}/{name}",
                        visited,
                        limit=limit,
                    )
                )
            else:
                items.append(
                    DriveFileItem(
                        path=f"{path_prefix}/{name}",
                        name=name,
                        mime_type=mime_type,
                        modified_time=str(file.get("modifiedTime", "")),
                        web_view_link=str(file.get("webViewLink", "")),
                    )
                )
            if len(items) >= limit:
                return items[:limit]

        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items[:limit]


def _load_drive_snapshot() -> tuple[list[DriveFileItem], str, str]:
    now = monotonic()
    cached_payload = _drive_snapshot_cache.get("payload")
    cached_at = float(_drive_snapshot_cache.get("fetched_at", 0.0))
    if cached_payload is not None and now - cached_at < _DRIVE_CACHE_TTL_SECONDS:
        return cached_payload

    service, detail = _build_drive_service()
    if service is None:
        payload = ([], "unavailable", detail)
        _drive_snapshot_cache["payload"] = payload
        _drive_snapshot_cache["fetched_at"] = now
        return payload

    try:
        roots = service.files().list(
            q=f"name='{JARVIS_ROOT}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)",
            pageSize=1,
        ).execute().get("files", [])
        if not roots:
            payload = ([], "missing", f"{JARVIS_ROOT} root folder not found")
            _drive_snapshot_cache["payload"] = payload
            _drive_snapshot_cache["fetched_at"] = now
            return payload

        root = roots[0]
        root_id = str(root.get("id", ""))
        files = _list_jarvis_drive_files(service, root_id, JARVIS_ROOT, set())
        payload = (files, "connected", detail)
        _drive_snapshot_cache["payload"] = payload
        _drive_snapshot_cache["fetched_at"] = now
        return payload
    except Exception as exc:
        logger.warning("Dashboard drive listing failed: %s", exc)
        payload = ([], "unavailable", f"unavailable: {exc}")
        _drive_snapshot_cache["payload"] = payload
        _drive_snapshot_cache["fetched_at"] = now
        return payload


def collect_snapshot(include_memories: bool = False, include_drive: bool = False) -> DashboardSnapshot:
    log_lines = _read_lines(LOG_PATH, limit=500)
    log_text = "\n".join(log_lines)
    recent_email_activity, processed_summary = _gmail_activity()
    memory_count, task_count, financial_count = _db_counts(DB_PATH)
    active_memories = _load_active_memories(DB_PATH)[0] if include_memories else []
    if include_drive:
        drive_files, drive_status, drive_detail = _load_drive_snapshot()
    else:
        drive_files, drive_status, drive_detail = [], "idle", "not loaded in this view"

    return DashboardSnapshot(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        app_status=_app_status(log_lines),
        memory_count=memory_count,
        task_count=task_count,
        financial_count=financial_count,
        connectivity=_connectivity_summary(log_text),
        recent_email_activity=recent_email_activity,
        recent_log_lines=log_lines[-40:],
        processed_summary=processed_summary,
        last_gmail_state=_gmail_state(),
        active_memories=active_memories,
        drive_status=drive_status,
        drive_detail=drive_detail,
        drive_files=drive_files,
    )


def _badge(status: str) -> str:
    cls = "ok" if status in {"connected", "present", "running"} else "muted"
    return f'<span class="badge {cls}">{html.escape(status)}</span>'


def _load_dashboard_logo_svg() -> str:
    if not LOGO_PATH.exists():
        return ""
    try:
        return LOGO_PATH.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as exc:
        logger.warning("Dashboard logo read failed: %s", exc)
        return ""


def _normalize_tab(tab: str) -> str:
    if tab in {"overview", "memory", "drive"}:
        return tab
    return "overview"


def _tab_nav(active_tab: str) -> str:
    links = [
        ("overview", "Overview"),
        ("memory", "Memory"),
        ("drive", "Drive"),
    ]
    return "\n".join(
        f'<button type="button" data-tab="{html.escape(key)}" class="tab {"active" if key == active_tab else ""}">{html.escape(label)}</button>'
        for key, label in links
    )


def _render_memory_rows(memories: list[MemoryItem]) -> str:
    if not memories:
        return """
        <tr>
          <td colspan="8" class="muted">No active memories found.</td>
        </tr>
        """

    rows = []
    for item in memories:
        document_ref = (
            f'<a href="https://drive.google.com/file/d/{html.escape(item.document_ref)}/view" target="_blank" rel="noreferrer">open</a>'
            if item.document_ref
            else "—"
        )
        rows.append(
            f"""
            <tr>
              <td>{html.escape(item.topic)}</td>
              <td>{html.escape(item.summary)}</td>
              <td>{html.escape(item.category)}</td>
              <td>{html.escape(item.source)}</td>
              <td>{html.escape(item.confidence)}</td>
              <td>{html.escape(item.created_at)}</td>
              <td>{html.escape(item.updated_at)}</td>
              <td>{document_ref}</td>
            </tr>
            """
        )
    return "\n".join(rows)


def _render_drive_rows(files: list[DriveFileItem]) -> str:
    if not files:
        return """
        <tr>
          <td colspan="5" class="muted">No Drive files found under the managed Drive root.</td>
        </tr>
        """

    rows = []
    for item in files:
        link = (
            f'<a href="{html.escape(item.web_view_link)}" target="_blank" rel="noreferrer">open</a>'
            if item.web_view_link
            else "—"
        )
        rows.append(
            f"""
            <tr>
              <td>{html.escape(item.path)}</td>
              <td>{html.escape(item.name)}</td>
              <td>{html.escape(item.mime_type)}</td>
              <td>{html.escape(item.modified_time)}</td>
              <td>{link}</td>
            </tr>
            """
        )
    return "\n".join(rows)


def _render_overview_content(snapshot: DashboardSnapshot) -> str:
    summary = snapshot.processed_summary
    connectivity_html = "\n".join(
        f"""
        <li>
          <div class=\"row\">
            <strong>{html.escape(item.name)}</strong>
            {_badge(item.status)}
          </div>
          <div class=\"muted\">{html.escape(item.detail)}</div>
        </li>
        """
        for item in snapshot.connectivity
    )
    recent_activity_rows = "\n".join(
        f"""
        <tr>
          <td>{html.escape(item.processed_at)}</td>
          <td>{html.escape(item.outcome)}</td>
          <td>{html.escape(item.sender)}</td>
          <td>{html.escape(item.subject)}</td>
          <td>{html.escape(item.reason)}</td>
        </tr>
        """
        for item in snapshot.recent_email_activity
    ) or """
        <tr>
          <td colspan="5" class="muted">No email activity recorded yet.</td>
        </tr>
    """
    recent_logs_html = "\n".join(f"<li><code>{html.escape(line)}</code></li>" for line in snapshot.recent_log_lines)
    return f"""
      <section class="panel">
        <h2>Connectivity</h2>
        <ul>{connectivity_html}</ul>
      </section>

      <section class="panel">
        <h2>Recent Email Activity</h2>
        <table>
          <thead>
            <tr>
              <th>Processed</th>
              <th>Outcome</th>
              <th>From</th>
              <th>Subject</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>{recent_activity_rows}</tbody>
        </table>
      </section>

      <section class="panel">
        <h2>Recent Log Lines</h2>
        <ul>{recent_logs_html}</ul>
      </section>
    """


def _render_memory_content(snapshot: DashboardSnapshot) -> str:
    return f"""
      <section class="panel">
        <h2>Active Memories</h2>
        <div class="muted">Read-only view of active rows in the memories table.</div>
        <table>
          <thead>
            <tr>
              <th>Topic</th>
              <th>Summary</th>
              <th>Category</th>
              <th>Source</th>
              <th>Confidence</th>
              <th>Created</th>
              <th>Updated</th>
              <th>Document ref</th>
            </tr>
          </thead>
          <tbody>{_render_memory_rows(snapshot.active_memories)}</tbody>
        </table>
      </section>
    """


def _render_drive_content(snapshot: DashboardSnapshot) -> str:
    return f"""
      <section class="panel">
        <h2>Drive Files</h2>
        <div class="muted">Status: {html.escape(snapshot.drive_status)} | {html.escape(snapshot.drive_detail)}</div>
        <table>
          <thead>
            <tr>
              <th>Path</th>
              <th>Name</th>
              <th>MIME type</th>
              <th>Modified</th>
              <th>Link</th>
            </tr>
          </thead>
          <tbody>{_render_drive_rows(snapshot.drive_files)}</tbody>
        </table>
      </section>
    """


def _render_summary_panel(snapshot: DashboardSnapshot) -> str:
    summary = snapshot.processed_summary
    return f"""
      <section class="panel">
        <h2>Overview</h2>
        <table>
          <tr><th>App status</th><td class="status">{html.escape(snapshot.app_status)}</td></tr>
          <tr><th>Memories</th><td>{html.escape(snapshot.memory_count)}</td></tr>
          <tr><th>Tasks</th><td>{html.escape(snapshot.task_count)}</td></tr>
          <tr><th>Financial records</th><td>{html.escape(snapshot.financial_count)}</td></tr>
          <tr><th>Gmail state</th><td class="muted">{html.escape(snapshot.last_gmail_state)}</td></tr>
          <tr><th>Email outcomes</th><td class="muted">Processed {sum(summary.values())} | Skipped {summary.get('skipped', 0)} | Filed {summary.get('filed', 0)} | Failed {summary.get('failed', 0)}</td></tr>
        </table>
      </section>
    """


def _render_tab_content(snapshot: DashboardSnapshot, tab: str) -> str:
    active_tab = _normalize_tab(tab)
    if active_tab == "memory":
        return _render_memory_content(snapshot)
    if active_tab == "drive":
        return _render_drive_content(snapshot)
    return _render_overview_content(snapshot)


def _render_snapshot(snapshot: DashboardSnapshot, tab: str = "overview") -> str:
    active_tab = _normalize_tab(tab)
    initial_summary = _render_summary_panel(snapshot)
    initial_tab_content = _render_tab_content(snapshot, active_tab)
    logo_svg = _load_dashboard_logo_svg()

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Marvis Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0e0e10;
      --surface: #1c1c21;
      --border: #2b2b31;
      --text: #f0f0f0;
      --muted: #9ca3af;
      --accent: #00ff9f;
      --accent-2: #00e0ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 16px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 20px;
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      font-weight: 600;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }}
    .brand-mark {{
      width: 30px;
      height: 30px;
      flex: 0 0 auto;
    }}
    .brand-mark svg {{
      display: block;
      width: 100%;
      height: 100%;
    }}
    .brand-copy {{
      min-width: 0;
    }}
    h2 {{
      margin: 0 0 12px 0;
      font-size: 15px;
      font-weight: 600;
    }}
    .subtle {{
      color: var(--muted);
    }}
    a {{
      color: var(--accent-2);
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .nav {{
      display: flex;
      gap: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 20px;
    }}
    .tab {{
      appearance: none;
      background: transparent;
      border: 0;
      cursor: pointer;
      color: var(--muted);
      font: inherit;
      padding-bottom: 4px;
      border-bottom: 1px solid transparent;
    }}
    .tab.active {{
      color: var(--text);
      border-bottom-color: var(--accent);
    }}
    .sections {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      padding: 16px;
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      text-align: left;
      padding: 8px 10px 8px 0;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 500;
    }}
    .status {{
      color: var(--accent);
      font-weight: 600;
    }}
    .status.muted {{
      color: var(--muted);
      font-weight: 400;
    }}
    ul {{
      list-style: none;
      padding: 0;
      margin: 0;
    }}
    li + li {{
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid var(--border);
    }}
    code {{
      white-space: pre-wrap;
      color: #d1d5db;
    }}
    .row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--border);
      padding: 2px 8px;
      font-size: 12px;
      color: var(--text);
    }}
    .badge.ok {{
      border-color: rgba(0, 255, 159, 0.35);
      color: var(--accent);
    }}
    .badge.muted {{
      color: var(--muted);
    }}
    .muted {{
      color: var(--muted);
      margin-top: 4px;
      word-break: break-word;
    }}
    @media (max-width: 1100px) {{
      .wrap {{
        padding: 20px;
      }}
    }}
    @media (max-width: 700px) {{
      header {{
        align-items: flex-start;
        flex-direction: column;
      }}
      .wrap {{
        padding: 16px;
      }}
    }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <header>
      <div class=\"brand\">
        <div class=\"brand-mark\">{logo_svg}</div>
        <div class=\"brand-copy\">
          <h1>Marvis Dashboard</h1>
        </div>
      </div>
      <div class=\"subtle\"><a href=\"/docs\">Docs</a> · Updated {html.escape(snapshot.generated_at)}</div>
    </header>
    <nav class=\"nav\">
      {_tab_nav(active_tab)}
    </nav>

    <div class=\"sections\">
      <div id="summary-panel">
        {initial_summary}
      </div>
      <div id="tab-content">
        {initial_tab_content}
      </div>
    </div>
  </div>
  <script>
    (() => {{
      const allowedTabs = new Set(["overview", "memory", "drive"]);
      const navButtons = Array.from(document.querySelectorAll(".tab"));
      const summaryPanel = document.getElementById("summary-panel");
      const tabContent = document.getElementById("tab-content");
      const cache = new Map();
      const initialTab = {json.dumps(active_tab)};
      let activeTab = initialTab;
      let summaryRequest = null;
      let currentTabRequest = null;
      let currentTabIntervalId = null;
      const pendingControllers = new Set();

      cache.set(initialTab, tabContent.innerHTML);

      function tabUrl(tab) {{
        return tab === "overview" ? "/" : "/?tab=" + encodeURIComponent(tab);
      }}

      function fragmentUrl(path) {{
        return new URL(path, window.location.href).toString();
      }}

      function trackController(controller) {{
        pendingControllers.add(controller);
        return controller;
      }}

      function untrackController(controller) {{
        pendingControllers.delete(controller);
      }}

      function abortPendingRequests() {{
        for (const controller of pendingControllers) {{
          controller.abort();
        }}
        pendingControllers.clear();
      }}

      function currentTabRefreshDelay(tab) {{
        return tab === "drive" ? 45000 : 15000;
      }}

      function scheduleCurrentTabRefresh() {{
        if (currentTabIntervalId !== null) {{
          window.clearInterval(currentTabIntervalId);
        }}
        currentTabIntervalId = window.setInterval(() => {{
          if (document.visibilityState !== "visible") {{
            return;
          }}
          void refreshCurrentTab();
        }}, currentTabRefreshDelay(activeTab));
      }}

      function setActiveTab(tab, updateHistory = true) {{
        activeTab = allowedTabs.has(tab) ? tab : "overview";
        for (const button of navButtons) {{
          button.classList.toggle("active", button.dataset.tab === activeTab);
        }}
        scheduleCurrentTabRefresh();
        if (updateHistory) {{
          window.history.replaceState({{ tab: activeTab }}, "", tabUrl(activeTab));
        }}
      }}

      async function fetchFragment(path, signal) {{
        const response = await window.fetch(fragmentUrl(path), {{
          headers: {{ "X-Requested-With": "fetch" }},
          credentials: "same-origin",
          signal,
        }});
        if (!response.ok) {{
          throw new Error("Request failed: " + response.status);
        }}
        return await response.text();
      }}

      async function loadTab(tab, force = false) {{
        const requestedTab = allowedTabs.has(tab) ? tab : "overview";
        setActiveTab(requestedTab);
        if (!force && cache.has(requestedTab)) {{
          tabContent.innerHTML = cache.get(requestedTab);
          return;
        }}
        tabContent.innerHTML = '<section class="panel"><div class="muted">Loading…</div></section>';
        const controller = trackController(new AbortController());
        try {{
          const fragment = await fetchFragment("/fragment/" + requestedTab, controller.signal);
          cache.set(requestedTab, fragment);
          if (activeTab === requestedTab) {{
            tabContent.innerHTML = fragment;
          }}
        }} catch (error) {{
          if (activeTab === requestedTab) {{
            tabContent.innerHTML = '<section class="panel"><div class="muted">Failed to load this tab.</div></section>';
          }}
          if (error.name !== "AbortError") {{
            console.error(error);
          }}
        }} finally {{
          untrackController(controller);
        }}
      }}

      async function refreshSummary() {{
        if (document.visibilityState !== "visible" || summaryRequest) {{
          return summaryRequest;
        }}
        const controller = trackController(new AbortController());
        summaryRequest = (async () => {{
          try {{
            summaryPanel.innerHTML = await fetchFragment("/fragment/summary", controller.signal);
          }} finally {{
            untrackController(controller);
            summaryRequest = null;
          }}
        }})();
        try {{
          await summaryRequest;
        }} catch (error) {{
          if (error.name !== "AbortError") {{
            console.error(error);
          }}
        }}
      }}

      async function refreshCurrentTab() {{
        const requestedTab = activeTab;
        if (document.visibilityState !== "visible" || currentTabRequest) {{
          return currentTabRequest;
        }}
        const controller = trackController(new AbortController());
        currentTabRequest = (async () => {{
          try {{
            const fragment = await fetchFragment("/fragment/" + requestedTab, controller.signal);
            cache.set(requestedTab, fragment);
            if (activeTab === requestedTab) {{
              tabContent.innerHTML = fragment;
            }}
          }} finally {{
            untrackController(controller);
            currentTabRequest = null;
          }}
        }})();
        try {{
          await currentTabRequest;
        }} catch (error) {{
          if (error.name !== "AbortError") {{
            console.error(error);
          }}
        }}
      }}

      function prefetchTab(tab, delayMs) {{
        if (!allowedTabs.has(tab) || tab === initialTab) {{
          return;
        }}
        window.setTimeout(async () => {{
          if (document.visibilityState !== "visible") {{
            return;
          }}
          if (cache.has(tab)) {{
            return;
          }}
          const controller = trackController(new AbortController());
          try {{
            cache.set(tab, await fetchFragment("/fragment/" + tab, controller.signal));
          }} catch (error) {{
            if (error.name !== "AbortError") {{
              console.error(error);
            }}
          }} finally {{
            untrackController(controller);
          }}
        }}, delayMs);
      }}

      for (const button of navButtons) {{
        button.addEventListener("click", () => {{
          void loadTab(button.dataset.tab);
        }});
      }}

      window.addEventListener("popstate", () => {{
        const params = new URLSearchParams(window.location.search);
        const tab = params.get("tab") || "overview";
        if (cache.has(tab)) {{
          setActiveTab(tab, false);
          tabContent.innerHTML = cache.get(tab);
          return;
        }}
        void loadTab(tab, false);
      }});

      window.addEventListener("pagehide", abortPendingRequests);
      window.addEventListener("beforeunload", abortPendingRequests);
      window.addEventListener("visibilitychange", () => {{
        if (document.visibilityState !== "visible") {{
          abortPendingRequests();
          return;
        }}
        void refreshSummary();
        void refreshCurrentTab();
      }});

      window.setInterval(() => {{
        if (document.visibilityState !== "visible") {{
          return;
        }}
        void refreshSummary();
      }}, 10000);

      scheduleCurrentTabRefresh();

      window.addEventListener("unload", () => {{
        if (currentTabIntervalId !== null) {{
          window.clearInterval(currentTabIntervalId);
        }}
      }});

      prefetchTab("memory", 500);
      prefetchTab("drive", 1500);
    }})();
  </script>
</body>
</html>"""


def _render_docs_page() -> str:
    if not DOCS_PATH.exists():
        return "<!doctype html><html><body><p>Docs page not found.</p></body></html>"
    try:
        return DOCS_PATH.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Dashboard docs read failed: %s", exc)
        return f"<!doctype html><html><body><p>Failed to load docs: {html.escape(str(exc))}</p></body></html>"


def _render_json(snapshot: DashboardSnapshot) -> str:
    payload = asdict(snapshot)
    payload["connectivity"] = [asdict(item) for item in snapshot.connectivity]
    payload["processed_summary"] = dict(snapshot.processed_summary)
    return json.dumps(payload, indent=2)


def _snapshot_for_tab(tab: str) -> DashboardSnapshot:
    active_tab = _normalize_tab(tab)
    return collect_snapshot(
        include_memories=active_tab == "memory",
        include_drive=active_tab == "drive",
    )


def _write_response(handler: BaseHTTPRequestHandler, body: bytes, content_type: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        logger.info("dashboard client disconnected before response completed")


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        tab = _normalize_tab(query.get("tab", ["overview"])[0])

        if parsed.path in {"/", "/index.html"}:
            snapshot = _snapshot_for_tab(tab)
            body = _render_snapshot(snapshot, tab=tab).encode("utf-8")
            _write_response(self, body, "text/html; charset=utf-8")
            return

        if parsed.path in {"/docs", "/docs/", "/docs/index.html"}:
            body = _render_docs_page().encode("utf-8")
            _write_response(self, body, "text/html; charset=utf-8")
            return

        if parsed.path == "/api/status":
            snapshot = _snapshot_for_tab(tab)
            body = _render_json(snapshot).encode("utf-8")
            _write_response(self, body, "application/json; charset=utf-8")
            return

        if parsed.path == "/fragment/summary":
            snapshot = collect_snapshot()
            body = _render_summary_panel(snapshot).encode("utf-8")
            _write_response(self, body, "text/html; charset=utf-8")
            return

        if parsed.path in {"/fragment/overview", "/fragment/memory", "/fragment/drive"}:
            fragment_tab = parsed.path.rsplit("/", 1)[-1]
            snapshot = _snapshot_for_tab(fragment_tab)
            body = _render_tab_content(snapshot, fragment_tab).encode("utf-8")
            _write_response(self, body, "text/html; charset=utf-8")
            return

        self.send_error(404, "Not Found")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        logger.info("dashboard: " + format, *args)


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    logger.info("Dashboard listening on http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Dashboard stopping")
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Marvis local dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    args = build_parser().parse_args()
    serve(args.host, args.port)
