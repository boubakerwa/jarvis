"""
LinkedIn draft queue — SQLite layer.

Tracks processing state: queue position, attempt count, errors, Obsidian artefact path.
Obsidian is write-only for the finished human-readable .md post note.

Table: linkedin_drafts
  id                   TEXT PK   — UUID
  status               TEXT      — pending_generation | ready | failed
  voice                TEXT
  origin               TEXT      — telegram | ui
  source_text          TEXT      — the raw input text
  source_author        TEXT
  source_url           TEXT
  source_type          TEXT      — manual | x-post
  rewrite_of           TEXT      — parent draft ID (rewrites only)
  rewrite_instructions TEXT
  preset_id            TEXT
  pillar_id            TEXT
  pillar_label         TEXT
  library_tags         TEXT      — JSON array
  attempts             INTEGER   DEFAULT 0
  last_error           TEXT
  last_attempt_at      TEXT
  obsidian_path        TEXT      — set once status=ready (vault-relative path)
  obsidian_filename    TEXT
  scheduled_for        TEXT      — UTC ISO timestamp for planned manual publishing
  publish_status       TEXT      — unscheduled | scheduled | published | archived
  published_at         TEXT
  linkedin_url         TEXT
  last_publish_reminder_at TEXT
  score_total          INTEGER
  score_json           TEXT      — JSON scoring rubric
  score_updated_at     TEXT
  verified_source_url  TEXT
  source_dossier_json  TEXT      — JSON verification notes
  link_policy          TEXT      — first_comment | inline | none
  created_at           TEXT
  updated_at           TEXT

Nothing in this table is about Wess personally — it's source text and post content only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from datetime import timedelta
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS linkedin_drafts (
    id                   TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'pending_generation',
    voice                TEXT NOT NULL DEFAULT 'professional',
    origin               TEXT NOT NULL DEFAULT 'telegram',
    source_text          TEXT NOT NULL,
    source_author        TEXT NOT NULL DEFAULT '',
    source_url           TEXT NOT NULL DEFAULT '',
    source_type          TEXT NOT NULL DEFAULT 'manual',
    rewrite_of           TEXT NOT NULL DEFAULT '',
    rewrite_instructions TEXT NOT NULL DEFAULT '',
    preset_id            TEXT NOT NULL DEFAULT '',
    pillar_id            TEXT NOT NULL DEFAULT '',
    pillar_label         TEXT NOT NULL DEFAULT '',
    library_tags         TEXT NOT NULL DEFAULT '[]',
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT NOT NULL DEFAULT '',
    last_attempt_at      TEXT NOT NULL DEFAULT '',
    obsidian_path        TEXT NOT NULL DEFAULT '',
    obsidian_filename    TEXT NOT NULL DEFAULT '',
    scheduled_for        TEXT NOT NULL DEFAULT '',
    publish_status       TEXT NOT NULL DEFAULT 'unscheduled',
    published_at         TEXT NOT NULL DEFAULT '',
    linkedin_url         TEXT NOT NULL DEFAULT '',
    last_publish_reminder_at TEXT NOT NULL DEFAULT '',
    score_total          INTEGER NOT NULL DEFAULT 0,
    score_json           TEXT NOT NULL DEFAULT '{}',
    score_updated_at     TEXT NOT NULL DEFAULT '',
    verified_source_url  TEXT NOT NULL DEFAULT '',
    source_dossier_json  TEXT NOT NULL DEFAULT '{}',
    link_policy          TEXT NOT NULL DEFAULT 'first_comment',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_li_status     ON linkedin_drafts(status);
CREATE INDEX IF NOT EXISTS idx_li_created    ON linkedin_drafts(created_at);
CREATE INDEX IF NOT EXISTS idx_li_rewrite_of ON linkedin_drafts(rewrite_of);
"""

_MIGRATION_COLUMNS: dict[str, str] = {
    "scheduled_for": "TEXT NOT NULL DEFAULT ''",
    "publish_status": "TEXT NOT NULL DEFAULT 'unscheduled'",
    "published_at": "TEXT NOT NULL DEFAULT ''",
    "linkedin_url": "TEXT NOT NULL DEFAULT ''",
    "last_publish_reminder_at": "TEXT NOT NULL DEFAULT ''",
    "score_total": "INTEGER NOT NULL DEFAULT 0",
    "score_json": "TEXT NOT NULL DEFAULT '{}'",
    "score_updated_at": "TEXT NOT NULL DEFAULT ''",
    "verified_source_url": "TEXT NOT NULL DEFAULT ''",
    "source_dossier_json": "TEXT NOT NULL DEFAULT '{}'",
    "link_policy": "TEXT NOT NULL DEFAULT 'first_comment'",
}

MAX_ATTEMPTS = 3
_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.JARVIS_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_CREATE_TABLE)
    _ensure_columns(conn)
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(linkedin_drafts)").fetchall()}
    for column, definition in _MIGRATION_COLUMNS.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE linkedin_drafts ADD COLUMN {column} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_li_publish_status ON linkedin_drafts(publish_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_li_scheduled_for ON linkedin_drafts(scheduled_for)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["library_tags"] = json.loads(d.get("library_tags") or "[]")
    except Exception:
        d["library_tags"] = []
    for key in ("score_json", "source_dossier_json"):
        parsed_key = key[:-5] if key.endswith("_json") else key
        try:
            d[parsed_key] = json.loads(d.get(key) or "{}")
        except Exception:
            d[parsed_key] = {}
    return d


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def enqueue(record: dict) -> dict:
    """
    Insert a new pending draft into SQLite.
    record is the output of composer.build_pending_record().
    Returns the stored row as a dict.
    """
    draft_id = record.get("id") or str(uuid.uuid4())
    now = _now()
    source = record.get("source", {})
    library = record.get("library", {})
    tags = json.dumps(library.get("tags", []))

    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO linkedin_drafts
                   (id, status, voice, origin,
                    source_text, source_author, source_url, source_type,
                    rewrite_of, rewrite_instructions, preset_id,
                    pillar_id, pillar_label, library_tags,
                    attempts, last_error, last_attempt_at,
                    obsidian_path, obsidian_filename, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'','','','',?,?)""",
                (
                    draft_id,
                    "pending_generation",
                    record.get("voice", "professional"),
                    record.get("origin", "telegram"),
                    source.get("text", ""),
                    source.get("author_name", ""),
                    source.get("url", ""),
                    source.get("type", "manual"),
                    library.get("parent_draft_id", ""),
                    library.get("rewrite_instructions", ""),
                    library.get("preset_id", ""),
                    library.get("pillar", {}).get("id", ""),
                    library.get("pillar", {}).get("label", ""),
                    tags,
                    now, now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM linkedin_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            logger.info("LinkedIn draft enqueued: %s", draft_id[:8])
            return _row_to_dict(row)
        finally:
            conn.close()


def mark_ready(draft_id: str, obsidian_path: str, obsidian_filename: str) -> None:
    """Mark a draft as successfully generated and saved to Obsidian."""
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """UPDATE linkedin_drafts
                   SET status='ready', obsidian_path=?, obsidian_filename=?, updated_at=?
                   WHERE id=?""",
                (obsidian_path, obsidian_filename, _now(), draft_id),
            )
            conn.commit()
        finally:
            conn.close()


def schedule_draft(draft_id: str, scheduled_for: str) -> Optional[dict]:
    """Schedule a ready draft for manual LinkedIn publishing."""
    cleaned = str(scheduled_for or "").strip()
    if not cleaned:
        raise ValueError("scheduled_for is required.")
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """UPDATE linkedin_drafts
                   SET scheduled_for=?,
                       publish_status='scheduled',
                       updated_at=?
                   WHERE id=?""",
                (cleaned, _now(), draft_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM linkedin_drafts WHERE id=?", (draft_id,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def clear_schedule(draft_id: str) -> Optional[dict]:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """UPDATE linkedin_drafts
                   SET scheduled_for='',
                       publish_status='unscheduled',
                       last_publish_reminder_at='',
                       updated_at=?
                   WHERE id=? AND publish_status!='published'""",
                (_now(), draft_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM linkedin_drafts WHERE id=?", (draft_id,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def mark_published(draft_id: str, *, linkedin_url: str = "", published_at: str | None = None) -> Optional[dict]:
    now = published_at or _now()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """UPDATE linkedin_drafts
                   SET publish_status='published',
                       published_at=?,
                       linkedin_url=?,
                       updated_at=?
                   WHERE id=?""",
                (now, str(linkedin_url or "").strip(), _now(), draft_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM linkedin_drafts WHERE id=?", (draft_id,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def archive_draft(draft_id: str) -> Optional[dict]:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """UPDATE linkedin_drafts
                   SET publish_status='archived',
                       updated_at=?
                   WHERE id=?""",
                (_now(), draft_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM linkedin_drafts WHERE id=?", (draft_id,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def update_source_verification(
    draft_id: str,
    *,
    verified_source_url: str = "",
    source_dossier: dict | None = None,
    link_policy: str = "",
) -> Optional[dict]:
    allowed_policies = {"", "first_comment", "inline", "none"}
    cleaned_policy = str(link_policy or "").strip()
    if cleaned_policy not in allowed_policies:
        raise ValueError("Unsupported link policy.")
    with _lock:
        conn = _get_conn()
        try:
            if cleaned_policy:
                conn.execute(
                    """UPDATE linkedin_drafts
                       SET verified_source_url=?,
                           source_dossier_json=?,
                           link_policy=?,
                           updated_at=?
                       WHERE id=?""",
                    (
                        str(verified_source_url or "").strip(),
                        json.dumps(source_dossier or {}, ensure_ascii=False),
                        cleaned_policy,
                        _now(),
                        draft_id,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE linkedin_drafts
                       SET verified_source_url=?,
                           source_dossier_json=?,
                           updated_at=?
                       WHERE id=?""",
                    (
                        str(verified_source_url or "").strip(),
                        json.dumps(source_dossier or {}, ensure_ascii=False),
                        _now(),
                        draft_id,
                    ),
                )
            conn.commit()
            row = conn.execute("SELECT * FROM linkedin_drafts WHERE id=?", (draft_id,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def update_score(draft_id: str, *, score_total: int, score: dict) -> Optional[dict]:
    bounded = max(0, min(100, int(score_total)))
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """UPDATE linkedin_drafts
                   SET score_total=?,
                       score_json=?,
                       score_updated_at=?,
                       updated_at=?
                   WHERE id=?""",
                (
                    bounded,
                    json.dumps(score or {}, ensure_ascii=False),
                    _now(),
                    _now(),
                    draft_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM linkedin_drafts WHERE id=?", (draft_id,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def mark_publish_reminded(draft_id: str, *, reminded_at: str | None = None) -> None:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """UPDATE linkedin_drafts
                   SET last_publish_reminder_at=?, updated_at=?
                   WHERE id=?""",
                (reminded_at or _now(), _now(), draft_id),
            )
            conn.commit()
        finally:
            conn.close()


def mark_attempt_failed(draft_id: str, error: str, *, permanent: bool = False) -> str:
    """
    Increment attempt counter, record error.
    Returns new status: 'pending_generation' (will retry) or 'failed' (exhausted).
    Pass permanent=True to skip retries and fail immediately (e.g. import errors).
    """
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT attempts FROM linkedin_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if not row:
                return "failed"

            new_attempts = (row["attempts"] or 0) + 1
            new_status = "failed" if (permanent or new_attempts >= MAX_ATTEMPTS) else "pending_generation"
            conn.execute(
                """UPDATE linkedin_drafts
                   SET attempts=?, last_error=?, last_attempt_at=?, status=?, updated_at=?
                   WHERE id=?""",
                (new_attempts, str(error)[:500], _now(), new_status, _now(), draft_id),
            )
            conn.commit()
            logger.info(
                "LinkedIn draft %s attempt %d → status=%s",
                draft_id[:8], new_attempts, new_status,
            )
            return new_status
        finally:
            conn.close()


def requeue_draft(draft_id: str, *, clear_artefacts: bool = False) -> Optional[dict]:
    """Reset a draft to pending_generation so it can be processed again."""
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM linkedin_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if not row:
                return None

            if clear_artefacts:
                conn.execute(
                    """UPDATE linkedin_drafts
                       SET status='pending_generation',
                           attempts=0,
                           last_error='',
                           last_attempt_at='',
                           obsidian_path='',
                           obsidian_filename='',
                           updated_at=?
                       WHERE id=?""",
                    (_now(), draft_id),
                )
            else:
                conn.execute(
                    """UPDATE linkedin_drafts
                       SET status='pending_generation',
                           attempts=0,
                           last_error='',
                           last_attempt_at='',
                           updated_at=?
                       WHERE id=?""",
                    (_now(), draft_id),
                )
            conn.commit()
            refreshed = conn.execute(
                "SELECT * FROM linkedin_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            logger.info("LinkedIn draft re-queued: %s", draft_id[:8])
            return _row_to_dict(refreshed) if refreshed else None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def list_pending(limit: int = 50) -> list[dict]:
    """
    Return drafts that need LLM processing:
      - status = 'pending_generation'
      - attempts < MAX_ATTEMPTS
    Oldest first (FIFO queue).
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM linkedin_drafts
               WHERE (status = 'pending_generation')
                 AND attempts < ?
               ORDER BY created_at ASC
               LIMIT ?""",
            (MAX_ATTEMPTS, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_drafts(
    limit: int = 20,
    status_filter: Optional[str] = None,
) -> list[dict]:
    """Return recent drafts, newest first. Optionally filter by status."""
    conn = _get_conn()
    try:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM linkedin_drafts WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM linkedin_drafts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_due_publish_reminders(*, now: datetime | None = None, limit: int = 20) -> list[dict]:
    current = now or datetime.now(timezone.utc)
    current_iso = current.astimezone(timezone.utc).isoformat()
    reminder_cutoff = (current.astimezone(timezone.utc) - timedelta(hours=24)).isoformat()
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM linkedin_drafts
               WHERE publish_status='scheduled'
                 AND scheduled_for!=''
                 AND scheduled_for <= ?
                 AND (last_publish_reminder_at='' OR last_publish_reminder_at <= ?)
               ORDER BY scheduled_for ASC
               LIMIT ?""",
            (current_iso, reminder_cutoff, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_by_id_prefix(prefix: str) -> Optional[dict]:
    """Find a draft by its UUID prefix (first 8 chars)."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM linkedin_drafts WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1",
            (prefix + "%",),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def get_by_id(draft_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM linkedin_drafts WHERE id=?", (draft_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def count_by_status() -> dict[str, int]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM linkedin_drafts GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
    finally:
        conn.close()
