from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from core.opslog import record_activity, record_issue
from core.structured_output import StructuredOutputError, generate_validated_json
from core.time_utils import get_local_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProposedCalendarEvent:
    summary: str
    start: str
    end: str = ""
    description: str = ""
    location: str = ""
    all_day: bool = False
    confidence: str = "medium"


@dataclass(frozen=True)
class ProposedTask:
    description: str
    due_date: str = ""
    confidence: str = "medium"


@dataclass(frozen=True)
class ProposedReminder:
    message: str
    when: str
    confidence: str = "medium"


@dataclass(frozen=True)
class ProposedMemoryUpdate:
    topic: str
    summary: str
    category: str = "fact"
    confidence: str = "medium"


@dataclass(frozen=True)
class EmailActionProposal:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    summary: str = ""
    rationale: str = ""
    calendar_events: tuple[ProposedCalendarEvent, ...] = ()
    tasks: tuple[ProposedTask, ...] = ()
    reminders: tuple[ProposedReminder, ...] = ()
    memory_updates: tuple[ProposedMemoryUpdate, ...] = ()
    reply_bullets: tuple[str, ...] = ()

    def has_actions(self) -> bool:
        return bool(
            self.calendar_events
            or self.tasks
            or self.reminders
            or self.memory_updates
            or self.reply_bullets
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_email_actions(email, *, now: datetime | None = None) -> EmailActionProposal | None:
    """Extract proposed future-facing actions from an email without committing side effects."""
    current = now or get_local_now()
    text = _email_context(email)
    if not text.strip():
        return None

    try:
        proposal = generate_validated_json(
            task="gmail_actions",
            max_tokens=1200,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Local now: {current.isoformat()}\n"
                        "Extract proposed actions from this email. Return only JSON.\n\n"
                        f"{text}"
                    ),
                }
            ],
            validator=lambda payload: _validate_payload(payload, email),
            allow_fallback=True,
        )
    except StructuredOutputError as exc:
        logger.warning("Gmail action extraction returned invalid output: %s", exc)
        record_issue(
            level="WARNING",
            event="gmail_action_extraction_invalid",
            component="gmail",
            status="warning",
            summary="Gmail action extraction returned invalid structured output",
            metadata={"message_id": email.message_id, "error": str(exc)[:300]},
        )
        return None
    except Exception as exc:
        logger.exception("Gmail action extraction failed")
        record_issue(
            level="ERROR",
            event="gmail_action_extraction_failed",
            component="gmail",
            status="error",
            summary="Gmail action extraction failed",
            metadata={"message_id": email.message_id, "error": str(exc)[:300]},
        )
        return None

    if not proposal.has_actions():
        return None

    record_activity(
        event="gmail_action_proposal_extracted",
        component="gmail",
        summary="Extracted proposed actions from Gmail message",
        metadata={
            "message_id": email.message_id,
            "calendar_events": len(proposal.calendar_events),
            "tasks": len(proposal.tasks),
            "reminders": len(proposal.reminders),
            "memory_updates": len(proposal.memory_updates),
            "reply_bullets": len(proposal.reply_bullets),
        },
    )
    return proposal


_SYSTEM_PROMPT = """You extract only explicit or strongly implied future-facing actions from email.
Return a compact JSON object with keys:
summary, rationale, calendar_events, tasks, reminders, memory_updates, reply_bullets.
Use empty arrays when there is no action. Do not invent facts.
Calendar event start/end values must be ISO-8601 when the email provides enough date/time detail.
Task due_date should be YYYY-MM-DD when known, otherwise empty.
Reminder when can be ISO-8601 or a short natural time expression only if explicit in the email.
Memory updates are durable facts/preferences/decisions worth remembering, with confidence low|medium|high.
Reply bullets are optional concise bullets for a possible response, not a full draft."""


def _email_context(email) -> str:
    body = " ".join((email.body or "").split())[:8000]
    attachment_names = ", ".join(a.filename for a in getattr(email, "attachments", [])[:10])
    parts = [
        f"From: {email.sender}",
        f"Subject: {email.subject}",
        f"Date: {email.date}",
    ]
    if attachment_names:
        parts.append(f"Attachments: {attachment_names}")
    if body:
        parts.append(f"Body: {body}")
    return "\n".join(parts)


def _validate_payload(payload: dict[str, Any], email) -> EmailActionProposal:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    return EmailActionProposal(
        message_id=str(email.message_id),
        thread_id=str(email.thread_id),
        sender=str(email.sender or ""),
        subject=str(email.subject or ""),
        summary=_clean(payload.get("summary"), 500),
        rationale=_clean(payload.get("rationale"), 500),
        calendar_events=tuple(_calendar_events(payload.get("calendar_events"))),
        tasks=tuple(_tasks(payload.get("tasks"))),
        reminders=tuple(_reminders(payload.get("reminders"))),
        memory_updates=tuple(_memory_updates(payload.get("memory_updates"))),
        reply_bullets=tuple(_clean(item, 240) for item in _as_list(payload.get("reply_bullets")) if _clean(item, 240)),
    )


def _calendar_events(value: Any) -> list[ProposedCalendarEvent]:
    events: list[ProposedCalendarEvent] = []
    for item in _as_list(value)[:5]:
        if not isinstance(item, dict):
            continue
        summary = _clean(item.get("summary"), 160)
        start = _clean(item.get("start"), 80)
        if not summary or not start:
            continue
        events.append(
            ProposedCalendarEvent(
                summary=summary,
                start=start,
                end=_clean(item.get("end"), 80),
                description=_clean(item.get("description"), 600),
                location=_clean(item.get("location"), 160),
                all_day=bool(item.get("all_day", False)),
                confidence=_confidence(item.get("confidence")),
            )
        )
    return events


def _tasks(value: Any) -> list[ProposedTask]:
    tasks: list[ProposedTask] = []
    for item in _as_list(value)[:8]:
        if not isinstance(item, dict):
            continue
        description = _clean(item.get("description"), 240)
        if description:
            tasks.append(
                ProposedTask(
                    description=description,
                    due_date=_clean(item.get("due_date"), 40),
                    confidence=_confidence(item.get("confidence")),
                )
            )
    return tasks


def _reminders(value: Any) -> list[ProposedReminder]:
    reminders: list[ProposedReminder] = []
    for item in _as_list(value)[:5]:
        if not isinstance(item, dict):
            continue
        message = _clean(item.get("message"), 240)
        when = _clean(item.get("when"), 120)
        if message and when:
            reminders.append(
                ProposedReminder(
                    message=message,
                    when=when,
                    confidence=_confidence(item.get("confidence")),
                )
            )
    return reminders


def _memory_updates(value: Any) -> list[ProposedMemoryUpdate]:
    updates: list[ProposedMemoryUpdate] = []
    for item in _as_list(value)[:6]:
        if not isinstance(item, dict):
            continue
        topic = _clean(item.get("topic"), 120)
        summary = _clean(item.get("summary"), 500)
        if topic and summary:
            updates.append(
                ProposedMemoryUpdate(
                    topic=topic,
                    summary=summary,
                    category=_category(item.get("category")),
                    confidence=_confidence(item.get("confidence")),
                )
            )
    return updates


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _confidence(value: Any) -> str:
    cleaned = str(value or "").strip().lower()
    return cleaned if cleaned in {"low", "medium", "high"} else "medium"


def _category(value: Any) -> str:
    cleaned = str(value or "").strip().lower()
    allowed = {"preference", "fact", "decision", "document_ref", "project", "household", "finance", "health", "task"}
    return cleaned if cleaned in allowed else "fact"
