"""LinkedIn editorial scheduling, scoring, and reminder helpers."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any

from core.opslog import record_activity, record_issue
from core.structured_output import StructuredOutputError, generate_validated_json
from core.time_utils import get_local_now, get_local_timezone

logger = logging.getLogger(__name__)

DEFAULT_LINK_POLICY = "first_comment"
DEFAULT_SLOT_WEEKDAYS = (1, 3)  # Tuesday, Thursday
DEFAULT_SLOT_TIME = time(hour=9, minute=0)


def parse_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Datetime value is required.")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=get_local_timezone())
    return parsed


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def local_display(value: str) -> str:
    if not value:
        return ""
    try:
        return parse_datetime(value).astimezone(get_local_timezone()).strftime("%a %d %b %H:%M")
    except Exception:
        return str(value)


def next_publish_slots(
    *,
    now: datetime | None = None,
    count: int = 8,
    occupied: set[str] | None = None,
) -> list[dict[str, str]]:
    """Return the next Tue/Thu 09:00 Berlin slots that are not occupied."""
    current = get_local_now(now)
    tz = current.tzinfo or get_local_timezone()
    occupied = occupied or set()
    slots: list[dict[str, str]] = []
    cursor = current.date()

    for offset in range(0, 90):
        day = cursor + timedelta(days=offset)
        if day.weekday() not in DEFAULT_SLOT_WEEKDAYS:
            continue
        local_slot = datetime.combine(day, DEFAULT_SLOT_TIME, tzinfo=tz)
        if local_slot <= current:
            continue
        utc_slot = to_utc_iso(local_slot)
        if utc_slot in occupied:
            continue
        slots.append(
            {
                "scheduled_for": utc_slot,
                "local_label": local_slot.strftime("%a %d %b %H:%M"),
                "date_label": local_slot.strftime("%A, %B %-d"),
                "time_label": local_slot.strftime("%H:%M"),
            }
        )
        if len(slots) >= count:
            break
    return slots


def build_source_dossier(row: dict, *, verified_source_url: str = "") -> dict[str, Any]:
    source_url = str(row.get("source_url") or "").strip()
    verified = str(verified_source_url or row.get("verified_source_url") or "").strip()
    return {
        "source_type": str(row.get("source_type") or ""),
        "source_author": str(row.get("source_author") or ""),
        "source_url": source_url,
        "verified_source_url": verified,
        "verification_state": "verified" if verified else "needs_verification",
    }


def score_draft(row: dict, *, content: str = "") -> dict[str, Any]:
    """Score a draft for executive-operator authority."""
    fallback = _heuristic_score(row, content=content)
    prompt = _score_prompt(row, content=content)
    try:
        score = generate_validated_json(
            task="linkedin_score",
            max_tokens=900,
            system=_SCORE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            validator=_validate_score,
            allow_fallback=True,
        )
        record_activity(
            event="linkedin_draft_scored",
            component="linkedin",
            summary="Scored LinkedIn draft for authority",
            metadata={"draft_id": str(row.get("id", ""))[:8], "score": score["total"]},
        )
        return score
    except Exception as exc:
        if not isinstance(exc, StructuredOutputError):
            logger.warning("LinkedIn scoring failed, using heuristic fallback: %s", exc)
        record_issue(
            level="WARNING",
            event="linkedin_scoring_fallback",
            component="linkedin",
            status="warning",
            summary="LinkedIn scoring used heuristic fallback",
            metadata={"draft_id": str(row.get("id", ""))[:8], "error": str(exc)[:300]},
        )
        return fallback


def format_publish_reminder(row: dict) -> str:
    title = str(row.get("obsidian_filename") or row.get("source_text") or row.get("id", "")[:8]).strip()
    score = int(row.get("score_total") or 0)
    scheduled = local_display(str(row.get("scheduled_for") or ""))
    source = str(row.get("verified_source_url") or row.get("source_url") or "").strip()
    lines = [
        "[LinkedIn] Scheduled post is due",
        f"Draft: {str(row.get('id', ''))[:8]}",
        f"Slot: {scheduled or 'now'}",
        f"Post: {_trim(title, 120)}",
    ]
    if score:
        lines.append(f"Authority score: {score}/100")
    if source:
        lines.append(f"Source: {source}")
    lines.append("Open the dashboard, review the copy, paste into LinkedIn, then mark it published.")
    return "\n".join(lines)


def process_publish_reminders(notifier=None, *, now: datetime | None = None) -> dict[str, Any]:
    from linkedin.sqlite_store import list_due_publish_reminders, mark_publish_reminded

    summary = {"sent": 0, "failed": 0, "skipped": 0}
    if notifier is None:
        summary["skipped"] = 1
        return summary

    due = list_due_publish_reminders(now=(now or datetime.now(timezone.utc)))
    for row in due:
        draft_id = str(row.get("id") or "")
        try:
            if notifier.send_message(format_publish_reminder(row)):
                mark_publish_reminded(draft_id)
                summary["sent"] += 1
            else:
                summary["failed"] += 1
        except Exception as exc:
            logger.warning("LinkedIn publish reminder failed for %s: %s", draft_id[:8], exc)
            summary["failed"] += 1
    if due:
        record_activity(
            event="linkedin_publish_reminders_checked",
            component="linkedin",
            summary=f"LinkedIn publish reminders checked: {summary['sent']} sent, {summary['failed']} failed",
            metadata=summary,
        )
    return summary


_SCORE_SYSTEM_PROMPT = """Score LinkedIn posts for an executive-operator audience.
Return only JSON with:
total, source_credibility, novelty_timeliness, executive_relevance,
differentiated_pov, clarity_linkedin_fit, strengths, risks, recommendation.
All rubric fields are integers. total must be 0-100."""


def _score_prompt(row: dict, *, content: str) -> str:
    return "\n\n".join(
        [
            "Rubric: source credibility 25, novelty/timeliness 20, executive relevance 20, differentiated point of view 20, clarity/LinkedIn fit 15.",
            f"Source type: {row.get('source_type')}",
            f"Source author: {row.get('source_author')}",
            f"Source URL: {row.get('source_url')}",
            f"Verified source URL: {row.get('verified_source_url')}",
            f"Pillar: {row.get('pillar_label')}",
            f"Draft content:\n{content or row.get('source_text') or ''}",
        ]
    )


def _validate_score(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "source_credibility": 25,
        "novelty_timeliness": 20,
        "executive_relevance": 20,
        "differentiated_pov": 20,
        "clarity_linkedin_fit": 15,
    }
    score: dict[str, Any] = {}
    total = 0
    for field, maximum in fields.items():
        value = max(0, min(maximum, int(payload.get(field, 0))))
        score[field] = value
        total += value
    reported_total = payload.get("total")
    if reported_total is not None:
        total = max(0, min(100, int(reported_total)))
    score["total"] = total
    score["strengths"] = _string_list(payload.get("strengths"), limit=3)
    score["risks"] = _string_list(payload.get("risks"), limit=3)
    score["recommendation"] = _trim(payload.get("recommendation"), 240)
    return score


def _heuristic_score(row: dict, *, content: str) -> dict[str, Any]:
    text = f"{row.get('source_text', '')}\n{content}".lower()
    verified = bool(str(row.get("verified_source_url") or row.get("source_url") or "").strip())
    source_credibility = 20 if verified else 10
    novelty = 16 if re.search(r"today|new|launch|released|paper|benchmark|frontier|open source", text) else 10
    executive = 16 if re.search(r"enterprise|workflow|operator|leader|team|cost|risk|adoption|strategy", text) else 11
    pov = 14 if re.search(r"i think|what matters|the lesson|my take|implication", text) else 9
    clarity = 12 if 350 <= len(content or row.get("source_text", "")) <= 1600 else 8
    total = source_credibility + novelty + executive + pov + clarity
    risks = []
    if not verified:
        risks.append("Add a verified source beyond the ingested post.")
    if pov < 12:
        risks.append("Sharpen the operator takeaway.")
    return {
        "total": total,
        "source_credibility": source_credibility,
        "novelty_timeliness": novelty,
        "executive_relevance": executive,
        "differentiated_pov": pov,
        "clarity_linkedin_fit": clarity,
        "strengths": ["Readable LinkedIn-native draft."],
        "risks": risks,
        "recommendation": "Review the hook, verify the source, and make the executive implication explicit.",
    }


def _string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_trim(item, 160) for item in value[:limit] if _trim(item, 160)]


def _trim(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
