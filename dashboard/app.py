from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import settings
from core.opslog import (
    ACTIVITY_RETENTION_SECONDS,
    AUDIT_RETENTION_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    ISSUES_RETENTION_SECONDS,
    OPS_ACTIVITY_PATH as DEFAULT_OPS_ACTIVITY_PATH,
    OPS_AUDIT_PATH as DEFAULT_OPS_AUDIT_PATH,
    OPS_ISSUES_PATH as DEFAULT_OPS_ISSUES_PATH,
    read_jsonl,
)
from storage.schema import JARVIS_ROOT

ROOT = Path(__file__).resolve().parents[1]
LOGO_PATH = ROOT / "dashboard" / "assets" / "marvis-mark.svg"
DOCS_PATH = ROOT / "docs" / "index.html"
LOG_PATH = ROOT / "logs" / "jarvis.log"
DB_PATH = ROOT / "data" / "jarvis_memory.db"
TOKEN_PATH = ROOT / "token.json"
GMAIL_STATE_PATH = ROOT / "data" / "gmail_state.txt"
GMAIL_ACTIVITY_PATH = ROOT / "data" / "gmail_activity.jsonl"
LLM_ACTIVITY_PATH = ROOT / "data" / "llm_activity.jsonl"
OPS_ACTIVITY_PATH = DEFAULT_OPS_ACTIVITY_PATH
OPS_ISSUES_PATH = DEFAULT_OPS_ISSUES_PATH
OPS_AUDIT_PATH = DEFAULT_OPS_AUDIT_PATH

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
    llmops_summary: "LLMOpsSummary"
    llmops_by_task: list["LLMTaskSummaryItem"]
    llmops_recent_calls: list["LLMCallItem"]
    llmops_cost_by_hour: list["ChartPoint"]
    ops_summary: "OpsSummary"
    ops_issue_components: list["OpsComponentItem"]
    ops_recent_issues: list["OpsEventItem"]
    ops_recent_audit: list["OpsEventItem"]
    ops_heartbeat_points: list["HeartbeatPoint"]
    linkedin_drafts: list["LinkedInDraftItem"] = None  # type: ignore[assignment]
    linkedin_draft_count: str = "—"

    def __post_init__(self) -> None:
        if self.linkedin_drafts is None:
            self.linkedin_drafts = []


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
class LinkedInDraftItem:
    draft_id: str
    headline: str
    hook: str
    full_post: str
    voice: str
    pillar_label: str
    tags: list[str]
    revision_number: int
    parent_draft_id: str
    source_label: str
    source_type: str
    generation_mode: str
    created_at: str
    updated_at: str
    status: str
    obsidian_path: str
    obsidian_filename: str
    attempts: int


@dataclass
class DriveFileItem:
    path: str
    name: str
    mime_type: str
    modified_time: str
    web_view_link: str


@dataclass
class LLMOpsSummary:
    call_count: int
    success_count: int
    avg_latency_ms: float
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None
    priced_call_count: int
    model_count: int
    task_count: int
    last_recorded_at: str


@dataclass
class LLMTaskSummaryItem:
    task: str
    call_count: int
    success_count: int
    avg_latency_ms: float
    total_tokens: int
    estimated_cost_usd: float | None


@dataclass
class LLMCallItem:
    recorded_at: str
    task: str
    model: str
    status: str
    latency_ms: float
    total_tokens: int
    estimated_cost_usd: float | None
    error: str


@dataclass
class ChartPoint:
    label: str
    value: float
    detail: str = ""


@dataclass
class OpsSummary:
    activity_count: int
    issue_count: int
    audit_count: int
    warning_count: int
    error_count: int
    heartbeat_status: str
    heartbeat_age_seconds: int | None
    last_activity_at: str
    last_issue_at: str
    last_audit_at: str


@dataclass
class OpsComponentItem:
    component: str
    warning_count: int
    error_count: int
    last_kind: str
    last_status: str
    last_event: str
    last_seen_at: str


@dataclass
class OpsEventItem:
    ts: str
    kind: str
    level: str
    component: str
    event: str
    status: str
    summary: str
    duration_ms: float


@dataclass
class HeartbeatPoint:
    ts: str
    age_seconds: int


def _read_lines(path: Path, limit: int = 200) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as exc:
        return [f"[dashboard] failed to read {path.name}: {exc}"]
    return [line.rstrip("\n") for line in lines[-limit:]]


def _parse_ts(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _app_status_from_logs(lines: list[str]) -> str:
    for line in reversed(lines):
        if "Application is stopping" in line:
            return "stopping"
        if "Application started" in line:
            return "running"
        if "Starting Marvis" in line or "Starting Jarvis" in line:
            return "starting"
    return "unknown"


def _event_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    parsed = _parse_ts(item.get("ts", ""))
    return (
        int(parsed.timestamp()) if parsed is not None else 0,
        str(item.get("ts", "")),
    )


def _render_ops_line(item: dict[str, Any]) -> str:
    duration = item.get("duration_ms")
    duration_text = ""
    if duration not in (None, ""):
        duration_text = f" ({_parse_float(duration):.1f} ms)"
    summary = str(item.get("summary", "")).strip() or str(item.get("event", "event"))
    return (
        f"{item.get('ts', '')} [{item.get('level', '')}] "
        f"{item.get('component', '')}:{item.get('event', '')} "
        f"{item.get('status', '')} {summary}{duration_text}"
    ).strip()


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


def _connectivity_summary(log_text: str, component_items: list[OpsComponentItem]) -> list[ConnectivityItem]:
    component_lookup = {item.component: item for item in component_items}
    checks = [
        ("Gmail", "gmail", "watcher thread active" if "Gmail watcher started" in log_text else "not seen recently"),
        ("Drive", "drive", "Drive initialized" if "Drive client initialised" in log_text else "not seen recently"),
        ("Calendar", "calendar", "Calendar initialized" if "Calendar client initialised" in log_text else "not seen recently"),
        ("Telegram", "telegram", "polling" if "Application started" in log_text else "not seen recently"),
    ]
    items = []
    for label, component_key, fallback_detail in checks:
        component = component_lookup.get(component_key)
        if component is None:
            status = "unknown"
            detail = fallback_detail
        elif component.last_kind == "issue" and component.last_status in {"warning", "error", "failed"}:
            status = "warning"
            detail = f"{component.last_event} at {component.last_seen_at}"
        else:
            status = "connected"
            detail = f"{component.last_event} at {component.last_seen_at}"
        items.append(ConnectivityItem(name=label, status=status, detail=detail))
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


def _parse_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _load_ops_snapshot(
    limit: int = 500,
) -> tuple[OpsSummary, list[OpsComponentItem], list[OpsEventItem], list[OpsEventItem], list[HeartbeatPoint], list[str]]:
    activity_payloads = read_jsonl(OPS_ACTIVITY_PATH, limit=limit)
    issue_payloads = read_jsonl(OPS_ISSUES_PATH, limit=limit)
    audit_payloads = read_jsonl(OPS_AUDIT_PATH, limit=limit)

    last_activity_at = str(activity_payloads[-1].get("ts", "")) if activity_payloads else ""
    last_issue_at = str(issue_payloads[-1].get("ts", "")) if issue_payloads else ""
    last_audit_at = str(audit_payloads[-1].get("ts", "")) if audit_payloads else ""

    last_heartbeat = next(
        (payload for payload in reversed(activity_payloads) if str(payload.get("event", "")) == "app_heartbeat"),
        None,
    )
    heartbeat_status = "missing"
    heartbeat_age_seconds: int | None = None
    if last_heartbeat is not None:
        heartbeat_at = _parse_ts(str(last_heartbeat.get("ts", "")))
        if heartbeat_at is not None:
            heartbeat_age_seconds = max(int((datetime.now(timezone.utc) - heartbeat_at).total_seconds()), 0)
            heartbeat_status = (
                "running" if heartbeat_age_seconds <= HEARTBEAT_INTERVAL_SECONDS * 2 else "stale"
            )

    heartbeat_points = [
        HeartbeatPoint(
            ts=str(payload.get("ts", "")),
            age_seconds=max(
                int((datetime.now(timezone.utc) - (_parse_ts(str(payload.get("ts", ""))) or datetime.now(timezone.utc))).total_seconds()),
                0,
            ),
        )
        for payload in activity_payloads
        if str(payload.get("event", "")) == "app_heartbeat"
    ][-12:]

    component_stats: dict[str, dict[str, Any]] = {}

    def touch_component(payload: dict[str, Any], *, is_issue: bool) -> None:
        component = str(payload.get("component", "")).strip() or "runtime"
        stats = component_stats.setdefault(
            component,
            {
                "warning_count": 0,
                "error_count": 0,
                "last_kind": "",
                "last_status": "",
                "last_event": "",
                "last_seen_at": "",
                "last_seen_key": (0, ""),
            },
        )
        level = str(payload.get("level", "")).upper()
        if is_issue:
            if level == "WARNING":
                stats["warning_count"] += 1
            else:
                stats["error_count"] += 1
        sort_key = _event_sort_key(payload)
        if sort_key >= stats["last_seen_key"]:
            stats["last_seen_key"] = sort_key
            stats["last_kind"] = str(payload.get("kind", ""))
            stats["last_status"] = str(payload.get("status", ""))
            stats["last_event"] = str(payload.get("event", ""))
            stats["last_seen_at"] = str(payload.get("ts", ""))

    for payload in activity_payloads:
        touch_component(payload, is_issue=False)
    for payload in audit_payloads:
        touch_component(payload, is_issue=False)
    for payload in issue_payloads:
        touch_component(payload, is_issue=True)

    component_items = [
        OpsComponentItem(
            component=component,
            warning_count=int(stats["warning_count"]),
            error_count=int(stats["error_count"]),
            last_kind=str(stats["last_kind"]),
            last_status=str(stats["last_status"]),
            last_event=str(stats["last_event"]),
            last_seen_at=str(stats["last_seen_at"]),
        )
        for component, stats in component_stats.items()
    ]
    component_items.sort(key=lambda item: (-(item.warning_count + item.error_count), item.component))

    recent_issue_items = [
        OpsEventItem(
            ts=str(payload.get("ts", "")),
            kind=str(payload.get("kind", "")),
            level=str(payload.get("level", "")),
            component=str(payload.get("component", "")),
            event=str(payload.get("event", "")),
            status=str(payload.get("status", "")),
            summary=str(payload.get("summary", "")),
            duration_ms=_parse_float(payload.get("duration_ms")),
        )
        for payload in reversed(issue_payloads[-12:])
    ]
    recent_audit_items = [
        OpsEventItem(
            ts=str(payload.get("ts", "")),
            kind=str(payload.get("kind", "")),
            level=str(payload.get("level", "")),
            component=str(payload.get("component", "")),
            event=str(payload.get("event", "")),
            status=str(payload.get("status", "")),
            summary=str(payload.get("summary", "")),
            duration_ms=_parse_float(payload.get("duration_ms")),
        )
        for payload in reversed(audit_payloads[-12:])
    ]

    recent_lines = [
        _render_ops_line(payload)
        for payload in sorted(activity_payloads[-20:] + issue_payloads[-20:], key=_event_sort_key)[-40:]
    ]

    summary = OpsSummary(
        activity_count=len(activity_payloads),
        issue_count=len(issue_payloads),
        audit_count=len(audit_payloads),
        warning_count=sum(1 for payload in issue_payloads if str(payload.get("level", "")).upper() == "WARNING"),
        error_count=sum(1 for payload in issue_payloads if str(payload.get("level", "")).upper() != "WARNING"),
        heartbeat_status=heartbeat_status,
        heartbeat_age_seconds=heartbeat_age_seconds,
        last_activity_at=last_activity_at,
        last_issue_at=last_issue_at,
        last_audit_at=last_audit_at,
    )
    return summary, component_items, recent_issue_items, recent_audit_items, heartbeat_points, recent_lines


def _load_llmops_activity(limit: int = 500) -> tuple[LLMOpsSummary, list[LLMTaskSummaryItem], list[LLMCallItem], list[ChartPoint]]:
    empty_summary = LLMOpsSummary(
        call_count=0,
        success_count=0,
        avg_latency_ms=0.0,
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        total_tokens=0,
        estimated_cost_usd=None,
        priced_call_count=0,
        model_count=0,
        task_count=0,
        last_recorded_at="",
    )
    if not LLM_ACTIVITY_PATH.exists():
        return empty_summary, [], [], []

    task_stats: dict[str, dict[str, Any]] = {}
    recent_calls: list[LLMCallItem] = []
    total_cost = 0.0
    priced_call_count = 0
    success_count = 0
    total_latency_ms = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_creation_tokens = 0
    total_cache_read_tokens = 0
    seen_models: set[str] = set()
    last_recorded_at = ""
    hourly_costs: dict[datetime, float] = {}
    last_recorded_dt: datetime | None = None

    for raw in _read_lines(LLM_ACTIVITY_PATH, limit=limit):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        task = str(payload.get("task", "")).strip() or "unknown"
        model = str(payload.get("model", "")).strip() or "unknown"
        status = str(payload.get("status", "")).strip() or "unknown"
        latency_ms = _parse_float(payload.get("latency_ms"))
        input_tokens = _parse_int(payload.get("input_tokens"))
        output_tokens = _parse_int(payload.get("output_tokens"))
        cache_creation_tokens = _parse_int(payload.get("cache_creation_input_tokens"))
        cache_read_tokens = _parse_int(payload.get("cache_read_input_tokens"))
        total_tokens = _parse_int(payload.get("total_tokens")) or (
            input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
        )
        estimated_cost = payload.get("estimated_cost_usd")
        estimated_cost_usd = None if estimated_cost in (None, "") else _parse_float(estimated_cost)
        recorded_at = str(payload.get("recorded_at", "")).strip()
        error = str(payload.get("error", "")).strip()
        recorded_dt = _parse_ts(recorded_at)

        if status == "ok":
            success_count += 1
        total_latency_ms += latency_ms
        total_input_tokens += input_tokens
        total_output_tokens += output_tokens
        total_cache_creation_tokens += cache_creation_tokens
        total_cache_read_tokens += cache_read_tokens
        seen_models.add(model)
        if recorded_at:
            last_recorded_at = recorded_at
        if recorded_dt is not None:
            if last_recorded_dt is None or recorded_dt > last_recorded_dt:
                last_recorded_dt = recorded_dt
        if estimated_cost_usd is not None:
            priced_call_count += 1
            total_cost += estimated_cost_usd
            if recorded_dt is not None:
                bucket = recorded_dt.replace(minute=0, second=0, microsecond=0)
                hourly_costs[bucket] = hourly_costs.get(bucket, 0.0) + estimated_cost_usd

        stats = task_stats.setdefault(
            task,
            {
                "call_count": 0,
                "success_count": 0,
                "latency_ms": 0.0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "priced_call_count": 0,
            },
        )
        stats["call_count"] += 1
        stats["latency_ms"] += latency_ms
        stats["total_tokens"] += total_tokens
        if status == "ok":
            stats["success_count"] += 1
        if estimated_cost_usd is not None:
            stats["estimated_cost_usd"] += estimated_cost_usd
            stats["priced_call_count"] += 1

        recent_calls.append(
            LLMCallItem(
                recorded_at=recorded_at,
                task=task,
                model=model,
                status=status,
                latency_ms=latency_ms,
                total_tokens=total_tokens,
                estimated_cost_usd=estimated_cost_usd,
                error=error,
            )
        )

    call_count = len(recent_calls)
    summary = LLMOpsSummary(
        call_count=call_count,
        success_count=success_count,
        avg_latency_ms=(total_latency_ms / call_count) if call_count else 0.0,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        cache_creation_input_tokens=total_cache_creation_tokens,
        cache_read_input_tokens=total_cache_read_tokens,
        total_tokens=total_input_tokens + total_output_tokens + total_cache_creation_tokens + total_cache_read_tokens,
        estimated_cost_usd=round(total_cost, 6) if priced_call_count else None,
        priced_call_count=priced_call_count,
        model_count=len(seen_models),
        task_count=len(task_stats),
        last_recorded_at=last_recorded_at,
    )
    task_items = [
        LLMTaskSummaryItem(
            task=task,
            call_count=stats["call_count"],
            success_count=stats["success_count"],
            avg_latency_ms=(stats["latency_ms"] / stats["call_count"]) if stats["call_count"] else 0.0,
            total_tokens=stats["total_tokens"],
            estimated_cost_usd=round(stats["estimated_cost_usd"], 6) if stats["priced_call_count"] else None,
        )
        for task, stats in task_stats.items()
    ]
    task_items.sort(key=lambda item: (-item.total_tokens, item.task))
    recent_calls = list(reversed(recent_calls[-20:]))
    cost_by_hour: list[ChartPoint] = []
    if last_recorded_dt is not None:
        final_hour = last_recorded_dt.replace(minute=0, second=0, microsecond=0)
        for offset in range(11, -1, -1):
            bucket_dt = final_hour - timedelta(hours=offset)
            label = bucket_dt.strftime("%H:%M")
            value = float(hourly_costs.get(bucket_dt, 0.0))
            cost_by_hour.append(ChartPoint(label=label, value=value, detail=_format_llm_cost(value)))
    return summary, task_items, recent_calls, cost_by_hour


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


def _load_linkedin_drafts_from_sqlite(limit: int = 20) -> tuple[list[LinkedInDraftItem], str]:
    """Load LinkedIn drafts from SQLite for the dashboard."""
    try:
        import sqlite3 as _sqlite3
        from config import settings as _settings
        conn = _sqlite3.connect(_settings.JARVIS_DB_PATH, check_same_thread=False)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM linkedin_drafts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("Dashboard LinkedIn SQLite load failed: %s", exc)
        return [], f"unavailable: {exc}"

    items: list[LinkedInDraftItem] = []
    for row in rows:
        r = dict(row)
        try:
            tags = json.loads(r.get("library_tags") or "[]")
        except Exception:
            tags = []
        items.append(
            LinkedInDraftItem(
                draft_id=str(r.get("id", ""))[:8],
                headline=str(r.get("obsidian_filename", "") or r.get("id", "")[:8]),
                hook="",
                full_post="",
                voice=str(r.get("voice", "")),
                pillar_label=str(r.get("pillar_label", "")),
                tags=tags,
                revision_number=1,
                parent_draft_id=str(r.get("rewrite_of", ""))[:8],
                source_label=str(r.get("source_author", "") or r.get("source_url", "")),
                source_type=str(r.get("source_type", "")),
                generation_mode="llm",
                created_at=str(r.get("created_at", ""))[:19].replace("T", " "),
                updated_at=str(r.get("updated_at", ""))[:19].replace("T", " "),
                status=str(r.get("status", "")),
                obsidian_path=str(r.get("obsidian_path", "")),
                obsidian_filename=str(r.get("obsidian_filename", "")),
                attempts=int(r.get("attempts", 0)),
            )
        )
    return items, f"loaded {len(items)} draft(s)"


def collect_snapshot(
    include_memories: bool = False,
    include_drive: bool = False,
    include_linkedin: bool = False,
) -> DashboardSnapshot:
    log_lines = _read_lines(LOG_PATH, limit=500)
    log_text = "\n".join(log_lines)
    recent_email_activity, processed_summary = _gmail_activity()
    memory_count, task_count, financial_count = _db_counts(DB_PATH)
    llmops_summary, llmops_by_task, llmops_recent_calls, llmops_cost_by_hour = _load_llmops_activity()
    (
        ops_summary,
        ops_issue_components,
        ops_recent_issues,
        ops_recent_audit,
        ops_heartbeat_points,
        ops_recent_lines,
    ) = _load_ops_snapshot()
    active_memories = _load_active_memories(DB_PATH)[0] if include_memories else []
    if include_drive:
        drive_files, drive_status, drive_detail = _load_drive_snapshot()
    else:
        drive_files, drive_status, drive_detail = [], "idle", "not loaded in this view"

    linkedin_drafts: list[LinkedInDraftItem] = []
    linkedin_draft_count = "—"
    if include_linkedin:
        linkedin_drafts, _ = _load_linkedin_drafts_from_sqlite()
        linkedin_draft_count = str(len(linkedin_drafts))

    app_status = "running" if ops_summary.heartbeat_status == "running" else _app_status_from_logs(log_lines)
    recent_log_lines = ops_recent_lines or log_lines[-40:]

    return DashboardSnapshot(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        app_status=app_status,
        memory_count=memory_count,
        task_count=task_count,
        financial_count=financial_count,
        connectivity=_connectivity_summary(log_text, ops_issue_components),
        recent_email_activity=recent_email_activity,
        recent_log_lines=recent_log_lines,
        processed_summary=processed_summary,
        last_gmail_state=_gmail_state(),
        active_memories=active_memories,
        drive_status=drive_status,
        drive_detail=drive_detail,
        drive_files=drive_files,
        llmops_summary=llmops_summary,
        llmops_by_task=llmops_by_task,
        llmops_recent_calls=llmops_recent_calls,
        llmops_cost_by_hour=llmops_cost_by_hour,
        ops_summary=ops_summary,
        ops_issue_components=ops_issue_components,
        ops_recent_issues=ops_recent_issues,
        ops_recent_audit=ops_recent_audit,
        ops_heartbeat_points=ops_heartbeat_points,
        linkedin_drafts=linkedin_drafts,
        linkedin_draft_count=linkedin_draft_count,
    )


def _badge(status: str) -> str:
    if status in {"connected", "present", "running"}:
        cls = "ok"
    elif status in {"warning", "partial", "stale"}:
        cls = "warn"
    else:
        cls = "muted"
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
    if tab in {"overview", "memory", "drive", "llmops", "linkedin"}:
        return tab
    return "overview"


def _tab_nav(active_tab: str) -> str:
    links = [
        ("overview", "Overview"),
        ("memory", "Memory"),
        ("drive", "Drive"),
        ("llmops", "LLMOps"),
        ("linkedin", "LinkedIn"),
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


def _format_llm_cost(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.6f}"


def _format_success_rate(success_count: int, call_count: int) -> str:
    if call_count <= 0:
        return "0%"
    return f"{(success_count / call_count) * 100:.1f}%"


def _format_retention_window(seconds: int) -> str:
    if seconds % (24 * 60 * 60) == 0:
        days = seconds // (24 * 60 * 60)
        return f"{days} day" + ("s" if days != 1 else "")
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    return f"{seconds} seconds"


def _format_age_seconds(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    seconds_remainder = seconds % 60
    return f"{minutes}m {seconds_remainder}s"


def _format_compact_number(value: float | int) -> str:
    number = float(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def _format_compact_cost(value: float) -> str:
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.01:
        return f"${value:.3f}"
    if value >= 0.001:
        return f"${value:.4f}"
    return f"${value:.5f}"


def _render_chart_card(title: str, body: str, note: str = "") -> str:
    note_html = f'<div class="chart-note">{html.escape(note)}</div>' if note else ""
    return f"""
      <div class="chart-card">
        <div class="chart-title">{html.escape(title)}</div>
        {body}
        {note_html}
      </div>
    """


def _render_empty_chart(title: str, message: str) -> str:
    return _render_chart_card(title, f'<div class="muted">{html.escape(message)}</div>')


def _render_line_chart(title: str, points: list[ChartPoint], note: str = "") -> str:
    if not points:
        return _render_empty_chart(title, "Not enough data yet.")

    width = 980
    height = 260
    margin_left = 62
    margin_right = 20
    margin_top = 16
    margin_bottom = 46
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom
    max_value = max(point.value for point in points) or 1.0
    step = plot_width / max(len(points) - 1, 1)

    def point_xy(index: int, value: float) -> tuple[float, float]:
        x = margin_left + step * index
        y = margin_top + plot_height - ((value / max_value) * plot_height if max_value else 0.0)
        return x, y

    line_points = " ".join(
        f"{x:.1f},{y:.1f}" for index, point in enumerate(points) for x, y in [point_xy(index, point.value)]
    )
    area_points = " ".join(
        [
            f"{margin_left:.1f},{margin_top + plot_height:.1f}",
            line_points,
            f"{margin_left + step * (len(points) - 1):.1f},{margin_top + plot_height:.1f}",
        ]
    )
    x_label_every = max(1, (len(points) - 1) // 4)
    x_labels = "\n".join(
        f'<text x="{point_xy(index, point.value)[0]:.1f}" y="{height - 12:.1f}" text-anchor="middle" class="chart-axis">{html.escape(point.label)}</text>'
        for index, point in enumerate(points)
        if index % x_label_every == 0 or index == len(points) - 1
    )
    y_labels = "\n".join(
        f'<text x="{margin_left - 8:.1f}" y="{margin_top + plot_height - ((tick / 4) * plot_height):.1f}" text-anchor="end" dominant-baseline="middle" class="chart-axis">{html.escape(_format_compact_cost(max_value * tick / 4) if "cost" in title.lower() else _format_compact_number(max_value * tick / 4))}</text>'
        for tick in range(5)
    )
    grid_lines = "\n".join(
        f'<line x1="{margin_left:.1f}" y1="{margin_top + plot_height - ((tick / 4) * plot_height):.1f}" x2="{width - margin_right:.1f}" y2="{margin_top + plot_height - ((tick / 4) * plot_height):.1f}" class="chart-grid-line" />'
        for tick in range(5)
    )
    dots = "\n".join(
        f"""
          <circle cx="{point_xy(index, point.value)[0]:.1f}" cy="{point_xy(index, point.value)[1]:.1f}" r="4" class="chart-dot">
            <title>{html.escape(point.label)}: {html.escape(point.detail or str(point.value))}</title>
          </circle>
        """
        for index, point in enumerate(points)
    )
    svg = f"""
      <svg viewBox="0 0 {width} {height}" class="chart-svg" role="img" aria-label="{html.escape(title)}">
        {grid_lines}
        {y_labels}
        <path d="M {area_points}" class="chart-area" />
        <polyline points="{line_points}" class="chart-line" />
        {dots}
        {x_labels}
      </svg>
    """
    return _render_chart_card(title, svg, note)


def _render_bar_chart(title: str, points: list[ChartPoint], note: str = "", bar_class: str = "chart-bar") -> str:
    if not points:
        return _render_empty_chart(title, "Not enough data yet.")

    width = 980
    row_height = 40
    height = 32 + row_height * len(points)
    margin_left = 190
    margin_right = 86
    bar_height = 16
    plot_width = width - margin_left - margin_right
    max_value = max(point.value for point in points) or 1.0

    rows = []
    for index, point in enumerate(points):
        y = 18 + index * row_height
        bar_width = (point.value / max_value) * plot_width if max_value else 0.0
        rows.append(
            f"""
              <text x="10" y="{y + 8:.1f}" class="chart-label">{html.escape(point.label)}</text>
              <rect x="{margin_left:.1f}" y="{y:.1f}" width="{plot_width:.1f}" height="{bar_height:.1f}" rx="4" class="chart-bar-bg" />
              <rect x="{margin_left:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" rx="4" class="{html.escape(bar_class)}">
                <title>{html.escape(point.label)}: {html.escape(point.detail or str(point.value))}</title>
              </rect>
              <text x="{width - 8:.1f}" y="{y + 8:.1f}" text-anchor="end" class="chart-value">{html.escape(point.detail or _format_compact_number(point.value))}</text>
            """
        )
    svg = f"""
      <svg viewBox="0 0 {width} {height}" class="chart-svg" role="img" aria-label="{html.escape(title)}">
        {''.join(rows)}
      </svg>
    """
    return _render_chart_card(title, svg, note)


def _render_issue_component_chart(items: list[OpsComponentItem]) -> str:
    chart_items = [
        item for item in items if (item.warning_count + item.error_count) > 0
    ][:6]
    if not chart_items:
        return _render_empty_chart("Issues by Component", "No recent warnings or errors.")

    width = 980
    row_height = 40
    height = 32 + row_height * len(chart_items)
    margin_left = 160
    margin_right = 94
    bar_height = 16
    plot_width = width - margin_left - margin_right
    max_value = max(item.warning_count + item.error_count for item in chart_items) or 1

    rows = []
    for index, item in enumerate(chart_items):
        warn_width = (item.warning_count / max_value) * plot_width if max_value else 0.0
        error_width = (item.error_count / max_value) * plot_width if max_value else 0.0
        y = 18 + index * row_height
        rows.append(
            f"""
              <text x="10" y="{y + 8:.1f}" class="chart-label">{html.escape(item.component)}</text>
              <rect x="{margin_left:.1f}" y="{y:.1f}" width="{plot_width:.1f}" height="{bar_height:.1f}" rx="4" class="chart-bar-bg" />
              <rect x="{margin_left:.1f}" y="{y:.1f}" width="{warn_width:.1f}" height="{bar_height:.1f}" rx="4" class="chart-bar-warn" />
              <rect x="{margin_left + warn_width:.1f}" y="{y:.1f}" width="{error_width:.1f}" height="{bar_height:.1f}" rx="4" class="chart-bar-error" />
              <text x="{width - 8:.1f}" y="{y + 8:.1f}" text-anchor="end" class="chart-value">{item.warning_count} warn / {item.error_count} err</text>
            """
        )
    svg = f"""
      <svg viewBox="0 0 {width} {height}" class="chart-svg" role="img" aria-label="Issues by component">
        {''.join(rows)}
      </svg>
    """
    return _render_chart_card("Issues by Component", svg, "Yellow shows warnings, red shows errors in the current 3-day issue window.")


def _render_heartbeat_timeline(points: list[HeartbeatPoint], generated_at: str) -> str:
    if not points:
        return _render_empty_chart("Heartbeat Timeline", "No heartbeat events in the current activity window.")

    width = 980
    height = 170
    margin_left = 32
    margin_right = 28
    baseline_y = 72
    label_y = 132
    plot_width = width - margin_left - margin_right
    end_dt = _parse_ts(generated_at) or datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(seconds=ACTIVITY_RETENTION_SECONDS)

    rendered_points = []
    previous_dt: datetime | None = None
    for point in points:
        ts = _parse_ts(point.ts)
        if ts is None:
            continue
        ratio = min(max((ts - start_dt).total_seconds() / ACTIVITY_RETENTION_SECONDS, 0.0), 1.0)
        x = margin_left + ratio * plot_width
        gap_seconds = int((ts - previous_dt).total_seconds()) if previous_dt is not None else 0
        dot_class = "chart-dot" if previous_dt is None or gap_seconds <= HEARTBEAT_INTERVAL_SECONDS * 2 else "chart-dot-warn"
        rendered_points.append(
            f"""
              <line x1="{x:.1f}" y1="{baseline_y - 14:.1f}" x2="{x:.1f}" y2="{baseline_y + 14:.1f}" class="chart-tick" />
              <circle cx="{x:.1f}" cy="{baseline_y:.1f}" r="6" class="{dot_class}">
                <title>{html.escape(point.ts)} | age {html.escape(_format_age_seconds(point.age_seconds))}</title>
              </circle>
            """
        )
        previous_dt = ts

    svg = f"""
      <svg viewBox="0 0 {width} {height}" class="chart-svg" role="img" aria-label="Heartbeat timeline">
        <line x1="{margin_left:.1f}" y1="{baseline_y:.1f}" x2="{width - margin_right:.1f}" y2="{baseline_y:.1f}" class="chart-grid-line" />
        <text x="{margin_left:.1f}" y="{label_y:.1f}" class="chart-axis">-{ACTIVITY_RETENTION_SECONDS // 60}m</text>
        <text x="{(width - margin_right + margin_left) / 2:.1f}" y="{label_y:.1f}" text-anchor="middle" class="chart-axis">heartbeat cadence</text>
        <text x="{width - margin_right:.1f}" y="{label_y:.1f}" text-anchor="end" class="chart-axis">now</text>
        {''.join(rendered_points)}
      </svg>
    """
    note = f"{len(points)} beat(s) retained over the last {_format_retention_window(ACTIVITY_RETENTION_SECONDS)}. Yellow dots indicate a stale gap."
    return _render_chart_card("Heartbeat Timeline", svg, note)


def _render_llmops_task_rows(items: list[LLMTaskSummaryItem]) -> str:
    if not items:
        return """
        <tr>
          <td colspan="6" class="muted">No LLM activity recorded yet.</td>
        </tr>
        """

    rows = []
    for item in items:
        rows.append(
            f"""
            <tr>
              <td>{html.escape(item.task)}</td>
              <td>{item.call_count}</td>
              <td>{html.escape(_format_success_rate(item.success_count, item.call_count))}</td>
              <td>{item.avg_latency_ms:.1f} ms</td>
              <td>{item.total_tokens}</td>
              <td>{html.escape(_format_llm_cost(item.estimated_cost_usd))}</td>
            </tr>
            """
        )
    return "\n".join(rows)


def _render_llmops_recent_rows(items: list[LLMCallItem]) -> str:
    if not items:
        return """
        <tr>
          <td colspan="7" class="muted">No recent LLM calls found.</td>
        </tr>
        """

    rows = []
    for item in items:
        detail = html.escape(item.error) if item.error else "—"
        rows.append(
            f"""
            <tr>
              <td>{html.escape(item.recorded_at)}</td>
              <td>{html.escape(item.task)}</td>
              <td>{html.escape(item.model)}</td>
              <td>{html.escape(item.status)}</td>
              <td>{item.latency_ms:.1f} ms</td>
              <td>{item.total_tokens}</td>
              <td>{html.escape(_format_llm_cost(item.estimated_cost_usd))}<div class="muted">{detail}</div></td>
            </tr>
            """
        )
    return "\n".join(rows)


def _render_ops_component_rows(items: list[OpsComponentItem]) -> str:
    if not items:
        return """
        <tr>
          <td colspan="6" class="muted">No operational issue history recorded yet.</td>
        </tr>
        """

    rows = []
    for item in items:
        rows.append(
            f"""
            <tr>
              <td>{html.escape(item.component)}</td>
              <td>{item.warning_count}</td>
              <td>{item.error_count}</td>
              <td>{html.escape(item.last_kind or "—")}</td>
              <td>{html.escape(item.last_event or "—")}</td>
              <td>{html.escape(item.last_seen_at or "—")}</td>
            </tr>
            """
        )
    return "\n".join(rows)


def _render_ops_event_rows(items: list[OpsEventItem], *, empty_message: str) -> str:
    if not items:
        return f"""
        <tr>
          <td colspan="6" class="muted">{html.escape(empty_message)}</td>
        </tr>
        """

    rows = []
    for item in items:
        duration_text = f"{item.duration_ms:.1f} ms" if item.duration_ms else "—"
        rows.append(
            f"""
            <tr>
              <td>{html.escape(item.ts)}</td>
              <td>{html.escape(item.component)}</td>
              <td>{html.escape(item.level or item.kind)}</td>
              <td>{html.escape(item.event)}</td>
              <td>{html.escape(item.status)}</td>
              <td>{html.escape(item.summary)}<div class="muted">{html.escape(duration_text)}</div></td>
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
        <h2>Recent Operational Events</h2>
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


def _render_llmops_content(snapshot: DashboardSnapshot) -> str:
    summary = snapshot.llmops_summary
    ops = snapshot.ops_summary
    token_points = [
        ChartPoint(
            label=item.task,
            value=float(item.total_tokens),
            detail=f"{_format_compact_number(item.total_tokens)} tok",
        )
        for item in snapshot.llmops_by_task[:6]
    ]
    return f"""
      <section class="panel">
        <h2>Charts</h2>
        <div class="chart-grid">
          {_render_line_chart("LLM Cost by Hour", snapshot.llmops_cost_by_hour, "Estimated spend across the most recent 12 hourly buckets with priced calls.")}
          {_render_bar_chart("Tokens by Task", token_points, "Top LLM tasks by token volume in the loaded activity window.")}
          {_render_issue_component_chart(snapshot.ops_issue_components)}
          {_render_heartbeat_timeline(snapshot.ops_heartbeat_points, snapshot.generated_at)}
        </div>
      </section>

      <section class="panel">
        <h2>LLMOps</h2>
        <div class="muted">Read-only telemetry from {html.escape(str(LLM_ACTIVITY_PATH.relative_to(ROOT)))}. Costs are estimated from local model price hints when available.</div>
        <table>
          <tr><th>Calls tracked</th><td>{summary.call_count}</td></tr>
          <tr><th>Success rate</th><td>{html.escape(_format_success_rate(summary.success_count, summary.call_count))}</td></tr>
          <tr><th>Average latency</th><td>{summary.avg_latency_ms:.1f} ms</td></tr>
          <tr><th>Input tokens</th><td>{summary.input_tokens}</td></tr>
          <tr><th>Output tokens</th><td>{summary.output_tokens}</td></tr>
          <tr><th>Cache write tokens</th><td>{summary.cache_creation_input_tokens}</td></tr>
          <tr><th>Cache read tokens</th><td>{summary.cache_read_input_tokens}</td></tr>
          <tr><th>Total tokens</th><td>{summary.total_tokens}</td></tr>
          <tr><th>Estimated cost</th><td>{html.escape(_format_llm_cost(summary.estimated_cost_usd))}</td></tr>
          <tr><th>Cost coverage</th><td>{summary.priced_call_count}/{summary.call_count} calls</td></tr>
          <tr><th>Models seen</th><td>{summary.model_count}</td></tr>
          <tr><th>Tasks seen</th><td>{summary.task_count}</td></tr>
          <tr><th>Last recorded</th><td class="muted">{html.escape(summary.last_recorded_at or "No LLM activity yet")}</td></tr>
        </table>
      </section>

      <section class="panel">
        <h2>Operational Logging</h2>
        <div class="muted">Positive activity is retained for {html.escape(_format_retention_window(ACTIVITY_RETENTION_SECONDS))}. Warnings and errors are retained for {html.escape(_format_retention_window(ISSUES_RETENTION_SECONDS))}. Minimal audit events are retained for {html.escape(_format_retention_window(AUDIT_RETENTION_SECONDS))}.</div>
        <table>
          <tr><th>Heartbeat</th><td>{html.escape(ops.heartbeat_status)} | {html.escape(_format_age_seconds(ops.heartbeat_age_seconds))} ago</td></tr>
          <tr><th>Activity events</th><td>{ops.activity_count}</td></tr>
          <tr><th>Issue events</th><td>{ops.issue_count}</td></tr>
          <tr><th>Warnings</th><td>{ops.warning_count}</td></tr>
          <tr><th>Errors</th><td>{ops.error_count}</td></tr>
          <tr><th>Audit events</th><td>{ops.audit_count}</td></tr>
          <tr><th>Last activity</th><td class="muted">{html.escape(ops.last_activity_at or "No activity yet")}</td></tr>
          <tr><th>Last issue</th><td class="muted">{html.escape(ops.last_issue_at or "No issues recorded")}</td></tr>
          <tr><th>Last audit</th><td class="muted">{html.escape(ops.last_audit_at or "No audit events recorded")}</td></tr>
        </table>
      </section>

      <section class="panel">
        <h2>Task Breakdown</h2>
        <table>
          <thead>
            <tr>
              <th>Task</th>
              <th>Calls</th>
              <th>Success</th>
              <th>Avg latency</th>
              <th>Tokens</th>
              <th>Est. cost</th>
            </tr>
          </thead>
          <tbody>{_render_llmops_task_rows(snapshot.llmops_by_task)}</tbody>
        </table>
      </section>

      <section class="panel">
        <h2>Recent Calls</h2>
        <table>
          <thead>
            <tr>
              <th>Recorded</th>
              <th>Task</th>
              <th>Model</th>
              <th>Status</th>
              <th>Latency</th>
              <th>Tokens</th>
              <th>Est. cost / detail</th>
            </tr>
          </thead>
          <tbody>{_render_llmops_recent_rows(snapshot.llmops_recent_calls)}</tbody>
        </table>
      </section>

      <section class="panel">
        <h2>Issue Breakdown</h2>
        <table>
          <thead>
            <tr>
              <th>Component</th>
              <th>Warnings</th>
              <th>Errors</th>
              <th>Last kind</th>
              <th>Last event</th>
              <th>Last seen</th>
            </tr>
          </thead>
          <tbody>{_render_ops_component_rows(snapshot.ops_issue_components)}</tbody>
        </table>
      </section>

      <section class="panel">
        <h2>Recent Issues</h2>
        <table>
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Component</th>
              <th>Level</th>
              <th>Event</th>
              <th>Status</th>
              <th>Summary</th>
            </tr>
          </thead>
          <tbody>{_render_ops_event_rows(snapshot.ops_recent_issues, empty_message="No warnings or errors recorded in the current retention window.")}</tbody>
        </table>
      </section>

      <section class="panel">
        <h2>Recent Audit Events</h2>
        <table>
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Component</th>
              <th>Level</th>
              <th>Event</th>
              <th>Status</th>
              <th>Summary</th>
            </tr>
          </thead>
          <tbody>{_render_ops_event_rows(snapshot.ops_recent_audit, empty_message="No recent audit events recorded.")}</tbody>
        </table>
      </section>
    """


def _render_summary_panel(snapshot: DashboardSnapshot) -> str:
    summary = snapshot.processed_summary
    llmops = snapshot.llmops_summary
    ops = snapshot.ops_summary
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
          <tr><th>LLM activity</th><td class="muted">Calls {llmops.call_count} | Tokens {llmops.total_tokens} | Success {_format_success_rate(llmops.success_count, llmops.call_count)} | Est. cost {html.escape(_format_llm_cost(llmops.estimated_cost_usd))}</td></tr>
          <tr><th>Ops logging</th><td class="muted">Heartbeat {html.escape(ops.heartbeat_status)} | Issues {ops.issue_count} | Audit {ops.audit_count} | Activity retention {_format_retention_window(ACTIVITY_RETENTION_SECONDS)}</td></tr>
          <tr><th>LinkedIn drafts</th><td class="muted">{html.escape(snapshot.linkedin_draft_count)} in queue</td></tr>
        </table>
      </section>
    """


def _render_linkedin_content(snapshot: DashboardSnapshot) -> str:
    drafts = snapshot.linkedin_drafts

    if not drafts:
        empty_html = """
        <div class="li-empty">
          <span class="li-tag">LINKEDIN COMPOSER</span>
          <p class="li-empty-text">No drafts yet.<br>
          Send <code>/linkedin &lt;text or URL&gt;</code> from Telegram to create your first draft.</p>
        </div>"""
    else:
        STATUS_ICON = {"ready": "✅", "pending_generation": "⏳", "failed": "❌"}
        cards_html = ""
        for item in drafts:
            tags_html = " ".join(
                f'<span class="li-inline-tag">{html.escape(t.upper())}</span>'
                for t in item.tags
            )
            parent_html = (
                f'<span class="li-inline-tag">REWRITE OF {html.escape(item.parent_draft_id)}</span>'
                if item.parent_draft_id else ""
            )
            status_icon = STATUS_ICON.get(item.status, "·")
            status_label = item.status.replace("_", " ").upper()
            obsidian_html = (
                f'<span class="li-meta-item">OBSIDIAN <span class="li-meta-value" title="{html.escape(item.obsidian_path)}">'
                f'{html.escape(item.obsidian_filename or "—")}</span></span>'
            )
            attempts_html = (
                f'<span class="li-inline-tag li-inline-tag--warn">ATTEMPTS {item.attempts}</span>'
                if item.attempts > 1 else ""
            )
            cards_html += f"""
            <div class="li-card li-card--{html.escape(item.status)}">
              <div class="li-card-header">
                <div class="li-card-meta">
                  <span class="li-tag">{html.escape(item.pillar_label.upper() or "LINKEDIN")}</span>
                  {parent_html}
                  {attempts_html}
                </div>
                <div class="li-card-status">{status_icon} <span class="li-status-label">{status_label}</span></div>
              </div>
              <h3 class="li-headline">{html.escape(item.headline)}</h3>
              <div class="li-meta-row">
                <span class="li-meta-item">VOICE <span class="li-meta-value">{html.escape(item.voice.upper())}</span></span>
                <span class="li-meta-sep">·</span>
                <span class="li-meta-item">SOURCE <span class="li-meta-value">{html.escape(item.source_type.upper())}</span></span>
                <span class="li-meta-sep">·</span>
                {obsidian_html}
                <span class="li-meta-sep">·</span>
                <span class="li-meta-item li-meta-ts">{html.escape(item.created_at)}</span>
              </div>
              {f'<div class="li-source-label">Source: {html.escape(item.source_label)}</div>' if item.source_label else ""}
              <div class="li-tags-row">{tags_html}</div>
            </div>"""
        empty_html = f'<div class="li-grid">{cards_html}</div>'

    return f"""
    <style>
      /* ── SolidTelco-derived LinkedIn section styles ───────────────────── */
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;510;590&display=swap');
      .li-root {{
        font-family: 'Inter', -apple-system, system-ui, Segoe UI, Roboto, sans-serif;
        font-feature-settings: "cv01", "ss03";
        background: #0d0f12;
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 4px;
        padding: 32px;
      }}
      .li-section-header {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 28px;
        padding-bottom: 16px;
        border-bottom: 1px solid rgba(255,255,255,0.06);
      }}
      .li-section-title {{
        font-size: 20px;
        font-weight: 510;
        color: #f0f1f3;
        letter-spacing: -0.24px;
        margin: 0;
      }}
      .li-count {{
        font-family: ui-monospace, SFMono-Regular, Roboto Mono, Menlo, Monaco, Courier New, monospace;
        font-size: 12px;
        font-weight: 400;
        letter-spacing: 0.8px;
        text-transform: uppercase;
        color: #7a808c;
      }}
      .li-tag {{
        display: inline-block;
        font-family: ui-monospace, SFMono-Regular, Roboto Mono, Menlo, Monaco, Courier New, monospace;
        font-size: 10px;
        font-weight: 400;
        letter-spacing: 1.2px;
        text-transform: uppercase;
        color: #00c8ff;
        background: rgba(0,200,255,0.08);
        border: 1px solid rgba(0,200,255,0.25);
        padding: 3px 8px;
        border-radius: 0;
      }}
      .li-inline-tag {{
        display: inline-block;
        font-family: ui-monospace, SFMono-Regular, Roboto Mono, Menlo, Monaco, Courier New, monospace;
        font-size: 10px;
        font-weight: 400;
        letter-spacing: 0.8px;
        text-transform: uppercase;
        color: #b8bdc6;
        background: rgba(255,255,255,0.06);
        border: 1px solid rgba(255,255,255,0.12);
        padding: 2px 6px;
        border-radius: 2px;
        margin-right: 4px;
      }}
      .li-inline-tag--accent {{
        color: #00c8ff;
        background: rgba(0,200,255,0.08);
        border-color: rgba(0,200,255,0.25);
      }}
      .li-grid {{
        display: grid;
        grid-template-columns: 1fr;
        gap: 16px;
      }}
      @media (min-width: 1100px) {{
        .li-grid {{ grid-template-columns: repeat(2, 1fr); }}
      }}
      .li-card {{
        background: #1c1f24;
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 4px;
        padding: 24px;
        transition: border-color 0.15s;
      }}
      .li-card:hover {{
        border-color: rgba(255,255,255,0.16);
      }}
      .li-card-header {{
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        margin-bottom: 14px;
        gap: 12px;
      }}
      .li-card-meta {{
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        align-items: center;
      }}
      .li-card-actions {{ flex-shrink: 0; }}
      .li-drive-link {{
        font-family: ui-monospace, SFMono-Regular, Roboto Mono, Menlo, Monaco, Courier New, monospace;
        font-size: 10px;
        font-weight: 400;
        letter-spacing: 1.4px;
        text-transform: uppercase;
        color: #00c8ff;
        text-decoration: none;
        border: 1px solid rgba(0,200,255,0.35);
        padding: 4px 10px;
        transition: background 0.15s;
      }}
      .li-drive-link:hover {{
        background: rgba(0,200,255,0.08);
        text-decoration: none;
      }}
      .li-headline {{
        font-size: 16px;
        font-weight: 510;
        color: #f0f1f3;
        letter-spacing: -0.1px;
        margin: 0 0 12px 0;
        line-height: 1.4;
      }}
      .li-meta-row {{
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        margin-bottom: 12px;
      }}
      .li-meta-item {{
        font-family: ui-monospace, SFMono-Regular, Roboto Mono, Menlo, Monaco, Courier New, monospace;
        font-size: 10px;
        font-weight: 400;
        letter-spacing: 0.8px;
        text-transform: uppercase;
        color: #7a808c;
      }}
      .li-meta-value {{
        color: #b8bdc6;
      }}
      .li-meta-sep {{ color: rgba(255,255,255,0.18); }}
      .li-meta-ts {{ text-transform: none; letter-spacing: 0; }}
      .li-source-label {{
        font-size: 12px;
        color: #7a808c;
        margin-bottom: 10px;
        font-style: italic;
      }}
      .li-hook {{
        font-size: 14px;
        font-weight: 400;
        color: #b8bdc6;
        line-height: 1.6;
        margin-bottom: 14px;
        border-left: 2px solid rgba(0,200,255,0.35);
        padding-left: 12px;
      }}
      .li-details {{
        margin-bottom: 12px;
      }}
      .li-details-summary {{
        font-family: ui-monospace, SFMono-Regular, Roboto Mono, Menlo, Monaco, Courier New, monospace;
        font-size: 10px;
        font-weight: 400;
        letter-spacing: 1.2px;
        text-transform: uppercase;
        color: #7a808c;
        cursor: pointer;
        user-select: none;
        outline: none;
      }}
      .li-details-summary:hover {{ color: #b8bdc6; }}
      .li-full-post {{
        margin: 10px 0 0 0;
        padding: 14px;
        background: #141619;
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 2px;
        font-size: 13px;
        color: #b8bdc6;
        line-height: 1.7;
        white-space: pre-wrap;
        word-break: break-word;
        font-family: inherit;
      }}
      .li-tags-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        margin-top: 4px;
      }}
      .li-warning {{
        margin-top: 10px;
        font-size: 12px;
        color: #f59e0b;
        background: rgba(245,158,11,0.08);
        border: 1px solid rgba(245,158,11,0.25);
        padding: 6px 10px;
        border-radius: 2px;
      }}
      .li-empty {{
        text-align: center;
        padding: 64px 32px;
      }}
      .li-empty-text {{
        color: #7a808c;
        font-size: 14px;
        line-height: 1.7;
        margin-top: 16px;
      }}
      .li-empty code {{
        font-family: ui-monospace, SFMono-Regular, Roboto Mono, Menlo, Monaco, Courier New, monospace;
        font-size: 13px;
        color: #00c8ff;
        background: rgba(0,200,255,0.08);
        padding: 2px 6px;
        border-radius: 2px;
      }}
    </style>
    <section class="panel li-root">
      <div class="li-section-header">
        <h2 class="li-section-title">LinkedIn Composer</h2>
        <span class="li-count">{len(drafts)} DRAFT{'S' if len(drafts) != 1 else ''} · JARVIS/PR/LINKEDIN COMPOSER</span>
      </div>
      {empty_html}
    </section>
    """


def _render_tab_content(snapshot: DashboardSnapshot, tab: str) -> str:
    active_tab = _normalize_tab(tab)
    if active_tab == "memory":
        return _render_memory_content(snapshot)
    if active_tab == "drive":
        return _render_drive_content(snapshot)
    if active_tab == "llmops":
        return _render_llmops_content(snapshot)
    if active_tab == "linkedin":
        return _render_linkedin_content(snapshot)
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
    .chart-grid {{
      display: grid;
      gap: 12px;
    }}
    .chart-card {{
      border: 1px solid var(--border);
      padding: 14px;
      background: rgba(255, 255, 255, 0.01);
      min-height: 280px;
    }}
    .chart-title {{
      margin-bottom: 10px;
      font-size: 14px;
      font-weight: 600;
    }}
    .chart-note {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 10px;
    }}
    .chart-svg {{
      width: 100%;
      height: auto;
      display: block;
      overflow: visible;
    }}
    .chart-axis {{
      fill: var(--muted);
      font-size: 12px;
    }}
    .chart-label {{
      fill: var(--text);
      font-size: 12px;
      dominant-baseline: middle;
    }}
    .chart-value {{
      fill: var(--muted);
      font-size: 12px;
      dominant-baseline: middle;
    }}
    .chart-grid-line {{
      stroke: rgba(255, 255, 255, 0.08);
      stroke-width: 1;
    }}
    .chart-line {{
      fill: none;
      stroke: var(--accent-2);
      stroke-width: 2.5;
      stroke-linejoin: round;
      stroke-linecap: round;
    }}
    .chart-area {{
      fill: rgba(0, 224, 255, 0.12);
      stroke: none;
    }}
    .chart-dot {{
      fill: var(--accent-2);
      stroke: var(--surface);
      stroke-width: 2;
    }}
    .chart-dot-warn {{
      fill: #ffbf47;
      stroke: var(--surface);
      stroke-width: 2;
    }}
    .chart-bar-bg {{
      fill: rgba(255, 255, 255, 0.06);
    }}
    .chart-bar {{
      fill: var(--accent);
    }}
    .chart-bar-warn {{
      fill: #ffbf47;
    }}
    .chart-bar-error {{
      fill: #ff6b6b;
    }}
    .chart-tick {{
      stroke: rgba(255, 255, 255, 0.14);
      stroke-width: 1;
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
    .badge.warn {{
      border-color: rgba(255, 191, 71, 0.35);
      color: #ffbf47;
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
    @media (min-width: 1200px) {{
      .chart-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
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
      .chart-card {{
        min-height: 240px;
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
      const allowedTabs = new Set(["overview", "memory", "drive", "llmops", "linkedin"]);
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
        if (tab === "drive" || tab === "linkedin") {{
          return 60000;
        }}
        if (tab === "llmops") {{
          return 20000;
        }}
        return 15000;
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
      prefetchTab("llmops", 2200);
      prefetchTab("linkedin", 3000);
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
        include_linkedin=active_tab == "linkedin",
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

        if parsed.path in {"/fragment/overview", "/fragment/memory", "/fragment/drive", "/fragment/llmops", "/fragment/linkedin"}:
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
