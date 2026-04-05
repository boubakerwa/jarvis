from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Iterator
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
OPS_ACTIVITY_PATH = ROOT / "data" / "ops_activity.jsonl"
OPS_ISSUES_PATH = ROOT / "data" / "ops_issues.jsonl"
OPS_AUDIT_PATH = ROOT / "data" / "ops_audit.jsonl"

ACTIVITY_RETENTION_SECONDS = 5 * 60
ISSUES_RETENTION_SECONDS = 3 * 24 * 60 * 60
AUDIT_RETENTION_SECONDS = 30 * 24 * 60 * 60
HEARTBEAT_INTERVAL_SECONDS = 60

_TIMESTAMP_KEY = "ts"
_PRUNE_COOLDOWN_SECONDS = 30.0
_WRITE_LOCK = Lock()
_LAST_PRUNE_AT: dict[str, float] = {}
_CURRENT_OP_ID: ContextVar[str] = ContextVar("jarvis_current_op_id", default="")

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"\b\d{7,}\b")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def current_timestamp() -> str:
    return utcnow().replace(microsecond=0).isoformat()


def new_op_id(prefix: str = "op") -> str:
    clean_prefix = re.sub(r"[^a-z0-9]+", "-", prefix.strip().lower()).strip("-") or "op"
    return f"{clean_prefix}-{uuid4().hex[:12]}"


def get_current_op_id() -> str:
    return _CURRENT_OP_ID.get("")


@contextmanager
def operation_context(op_id: str) -> Iterator[str]:
    token = _CURRENT_OP_ID.set(op_id)
    try:
        yield op_id
    finally:
        _CURRENT_OP_ID.reset(token)


def _safe_text(value: Any, limit: int = 280) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _EMAIL_RE.sub("[redacted-email]", text)
    text = _LONG_DIGITS_RE.sub("[redacted-id]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _normalize_component(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "runtime"
    return text.split(".", 1)[0]


def _retention_for_kind(kind: str) -> int:
    if kind == "issue":
        return ISSUES_RETENTION_SECONDS
    if kind == "audit":
        return AUDIT_RETENTION_SECONDS
    return ACTIVITY_RETENTION_SECONDS


def _path_for_kind(kind: str) -> Path:
    if kind == "issue":
        return OPS_ISSUES_PATH
    if kind == "audit":
        return OPS_AUDIT_PATH
    return OPS_ACTIVITY_PATH


def _parse_ts(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_jsonl(path: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def _prune_locked(path: Path, retention_seconds: int) -> None:
    if not path.exists():
        return
    now = utcnow()
    cutoff = now - timedelta(seconds=retention_seconds)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return

    kept: list[str] = []
    for raw in lines:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_time = _parse_ts(payload.get(_TIMESTAMP_KEY, ""))
        if event_time is None or event_time >= cutoff:
            kept.append(json.dumps(payload, ensure_ascii=False) + "\n")

    try:
        with path.open("w", encoding="utf-8") as handle:
            handle.writelines(kept)
    except Exception:
        return


def _maybe_prune_locked(path: Path, retention_seconds: int) -> None:
    now = monotonic()
    path_key = str(path)
    last_pruned_at = _LAST_PRUNE_AT.get(path_key)
    if last_pruned_at is not None and now - last_pruned_at < _PRUNE_COOLDOWN_SECONDS:
        return
    _prune_locked(path, retention_seconds)
    _LAST_PRUNE_AT[path_key] = now


def write_event(
    *,
    kind: str,
    level: str,
    event: str,
    component: str,
    status: str = "ok",
    summary: str = "",
    duration_ms: float | int | None = None,
    op_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    path = _path_for_kind(kind)
    payload: dict[str, Any] = {
        _TIMESTAMP_KEY: current_timestamp(),
        "kind": kind,
        "level": str(level or "INFO").upper(),
        "event": _safe_text(event, limit=120) or "event",
        "component": _normalize_component(component),
        "status": _safe_text(status, limit=80) or "ok",
        "summary": _safe_text(summary, limit=500),
    }
    current_op_id = op_id or get_current_op_id()
    if current_op_id:
        payload["op_id"] = _safe_text(current_op_id, limit=80)
    if duration_ms is not None:
        try:
            payload["duration_ms"] = round(float(duration_ms), 2)
        except (TypeError, ValueError):
            pass
    if metadata:
        payload["metadata"] = metadata

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False) + "\n"
        with _WRITE_LOCK:
            _maybe_prune_locked(path, _retention_for_kind(kind))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
    except Exception as exc:
        sys.stderr.write(f"[opslog] failed to write {kind} event: {exc}\n")


def record_activity(
    *,
    event: str,
    component: str,
    status: str = "ok",
    summary: str = "",
    duration_ms: float | int | None = None,
    op_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    write_event(
        kind="activity",
        level="INFO",
        event=event,
        component=component,
        status=status,
        summary=summary,
        duration_ms=duration_ms,
        op_id=op_id,
        metadata=metadata,
    )


def record_issue(
    *,
    level: str = "ERROR",
    event: str,
    component: str,
    status: str = "error",
    summary: str = "",
    duration_ms: float | int | None = None,
    op_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    write_event(
        kind="issue",
        level=level,
        event=event,
        component=component,
        status=status,
        summary=summary,
        duration_ms=duration_ms,
        op_id=op_id,
        metadata=metadata,
    )


def record_audit(
    *,
    event: str,
    component: str,
    status: str = "ok",
    summary: str = "",
    op_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    write_event(
        kind="audit",
        level="INFO",
        event=event,
        component=component,
        status=status,
        summary=summary,
        op_id=op_id,
        metadata=metadata,
    )


class IssuePersistenceHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        if record.name.startswith("core.opslog"):
            return

        event = getattr(record, "ops_event", "") or f"log.{record.levelname.lower()}"
        component = getattr(record, "ops_component", "") or _normalize_component(record.name)
        status = getattr(record, "ops_status", "") or "error"
        metadata = dict(getattr(record, "ops_metadata", {}) or {})
        if record.exc_info:
            metadata.setdefault("exception_type", getattr(record.exc_info[0], "__name__", "Exception"))
            metadata.setdefault(
                "traceback",
                _safe_text("".join(traceback.format_exception(*record.exc_info)), limit=1500),
            )
        record_issue(
            level=record.levelname,
            event=event,
            component=component,
            status=status,
            summary=record.getMessage(),
            duration_ms=getattr(record, "ops_duration_ms", None),
            op_id=getattr(record, "ops_op_id", None),
            metadata=metadata or None,
        )
