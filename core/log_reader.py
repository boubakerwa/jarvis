from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.opslog import OPS_ACTIVITY_PATH, OPS_AUDIT_PATH, OPS_ISSUES_PATH, read_jsonl
from core.time_utils import get_local_now, get_local_timezone, resolve_date_expression


_LOG_SOURCES: tuple[tuple[str, Path], ...] = (
    ("ops_activity", OPS_ACTIVITY_PATH),
    ("ops_issues", OPS_ISSUES_PATH),
    ("ops_audit", OPS_AUDIT_PATH),
)


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


def _normalize_level(value: str | None) -> str | None:
    text = str(value or "").strip().upper()
    if not text or text == "ALL":
        return None
    return text


def _normalize_record(source: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    ts = _parse_ts(payload.get("ts", ""))
    if ts is None:
        return None

    return {
        "source": source,
        "ts": ts.isoformat(),
        "kind": str(payload.get("kind", "")).strip(),
        "level": str(payload.get("level", "INFO")).upper(),
        "component": str(payload.get("component", "")).strip(),
        "event": str(payload.get("event", "")).strip(),
        "status": str(payload.get("status", "")).strip(),
        "summary": str(payload.get("summary", "")).strip(),
        "metadata": payload.get("metadata"),
        "op_id": str(payload.get("op_id", "")).strip(),
        "duration_ms": payload.get("duration_ms"),
    }


def read_logs(
    *,
    date_expression: str | None = None,
    level: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    normalized_level = _normalize_level(level)
    capped_limit = max(1, min(int(limit or 20), 200))
    target_date = None
    if date_expression:
        target_date = resolve_date_expression(date_expression, now=get_local_now())

    records: list[dict[str, Any]] = []
    for source, path in _LOG_SOURCES:
        for payload in read_jsonl(path, limit=max(capped_limit * 5, 100)):
            record = _normalize_record(source, payload)
            if record is None:
                continue
            if normalized_level and record["level"] != normalized_level:
                continue
            if target_date is not None:
                local_date = _parse_ts(record["ts"]).astimezone(get_local_timezone()).date()
                if local_date != target_date:
                    continue
            records.append(record)

    records.sort(key=lambda item: item["ts"], reverse=True)
    return records[:capped_limit]
