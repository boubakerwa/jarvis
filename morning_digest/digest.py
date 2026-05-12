"""Daily operating picture builder and scheduler."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from config import settings
from core.opslog import new_op_id, operation_context, record_activity, record_issue
from core.structured_output import generate_validated_json
from core.time_utils import get_local_now

if TYPE_CHECKING:
    from telegram_bot.bot import TelegramProactiveNotifier

logger = logging.getLogger(__name__)
_GMAIL_ACTIVITY_FILE = "data/gmail_activity.jsonl"

_MOTIVATIONAL_NOTES = [
    "Every line of code you write today is a step closer to the version of Marvis you're building.",
    "Ship something small, learn something new, and keep the momentum going.",
    "Focus beats busyness - one meaningful task done well is worth ten half-finished ones.",
    "The best time to fix a bug is right now, before it becomes someone else's emergency.",
    "Progress, not perfection - keep moving.",
    "Small consistent steps build great systems.",
    "Today's curiosity is tomorrow's feature.",
    "Code is communication - write it for the next person, even if that person is future you.",
    "Pick the one task that, if done today, makes everything else easier.",
    "Start with the hardest thing first; the rest of the day will feel like a gift.",
]


@dataclass(frozen=True)
class PictureItem:
    source: str
    title: str
    detail: str = ""
    priority: int = 50


def _pick_motivational_note(now: datetime) -> str:
    day_of_year = now.timetuple().tm_yday
    return _MOTIVATIONAL_NOTES[day_of_year % len(_MOTIVATIONAL_NOTES)]


def build_morning_message(
    now: datetime | None = None,
    *,
    memory_manager=None,
    reminder_manager=None,
    calendar_client=None,
) -> str:
    return build_daily_operating_picture(
        now=now,
        memory_manager=memory_manager,
        reminder_manager=reminder_manager,
        calendar_client=calendar_client,
    )


def build_daily_operating_picture(
    now: datetime | None = None,
    *,
    memory_manager=None,
    reminder_manager=None,
    calendar_client=None,
) -> str:
    now = now or get_local_now()
    day_str = now.strftime("%A, %B %-d")
    window_hours = max(1, settings.JARVIS_DAILY_PICTURE_WINDOW_HOURS)

    calendar_events = _fetch_calendar_events(calendar_client, now, window_hours)
    tasks = _fetch_tasks(memory_manager, limit=8)
    reminders = _fetch_reminders(reminder_manager, limit=8)
    gmail_activity = _read_recent_gmail_activity(limit=8)
    linkedin_drafts = _fetch_linkedin_drafts(limit=6)
    issues = _fetch_open_issues(limit=10)
    ranked = _rank_items(
        _candidate_items(
            calendar_events=calendar_events,
            tasks=tasks,
            reminders=reminders,
            gmail_activity=gmail_activity,
            linkedin_drafts=linkedin_drafts,
            issues=issues,
            now=now,
        )
    )

    lines: list[str] = [
        f"Good morning, Wess. Daily operating picture for {day_str}.",
        "",
    ]

    if ranked:
        lines.append("If you only do three things:")
        for item in _llm_order_top_three(ranked[:8])[:3]:
            reason = f" - {item.detail}" if item.detail else ""
            lines.append(f"- {item.title}{reason}")
        lines.append("")
    else:
        lines.extend(["If you only do three things:", "- No urgent cross-system items found.", ""])

    lines.extend(_format_calendar_section(calendar_events, window_hours))
    lines.extend(_format_task_section(tasks))
    lines.extend(_format_reminder_section(reminders))
    lines.extend(_format_gmail_section(gmail_activity))
    lines.extend(_format_linkedin_section(linkedin_drafts))
    lines.extend(_format_github_section(issues))
    lines.append(_pick_motivational_note(now))
    return "\n".join(lines)


def _fetch_open_issues(limit: int = 10) -> list:
    try:
        from github_issues.client import GitHubIssuesClient, load_github_client_config

        config = load_github_client_config()
        client = GitHubIssuesClient(config)
        return client.list_issues(state="open", limit=limit)
    except Exception as exc:
        logger.debug("GitHub issues unavailable for daily picture: %s", exc)
        return []


def _fetch_calendar_events(calendar_client, now: datetime, window_hours: int) -> list[dict]:
    if not calendar_client:
        return []
    try:
        end = now + timedelta(hours=window_hours)
        return calendar_client.get_events(now.isoformat(), end.isoformat(), max_results=12)
    except Exception as exc:
        logger.debug("Calendar unavailable for daily picture: %s", exc)
        return []


def _fetch_tasks(memory_manager, limit: int) -> list[dict]:
    if not memory_manager:
        return []
    try:
        return memory_manager.list_tasks("pending")[:limit]
    except Exception as exc:
        logger.debug("Tasks unavailable for daily picture: %s", exc)
        return []


def _fetch_reminders(reminder_manager, limit: int) -> list[dict]:
    if not reminder_manager:
        return []
    try:
        return reminder_manager.list_reminders("scheduled")[:limit]
    except Exception as exc:
        logger.debug("Reminders unavailable for daily picture: %s", exc)
        return []


def _fetch_linkedin_drafts(limit: int) -> list[dict]:
    try:
        from linkedin.sqlite_store import list_drafts

        drafts = list_drafts(limit=limit)
        return [draft for draft in drafts if draft.get("status") in {"pending_generation", "ready", "failed"}]
    except Exception as exc:
        logger.debug("LinkedIn drafts unavailable for daily picture: %s", exc)
        return []


def _read_recent_gmail_activity(limit: int) -> list[dict]:
    if not os.path.exists(_GMAIL_ACTIVITY_FILE):
        return []
    try:
        with open(_GMAIL_ACTIVITY_FILE, "r", encoding="utf-8", errors="replace") as handle:
            rows = []
            for raw in handle.readlines()[-limit:]:
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
            return rows
    except Exception as exc:
        logger.debug("Gmail activity unavailable for daily picture: %s", exc)
        return []


def _candidate_items(
    *,
    calendar_events: list[dict],
    tasks: list[dict],
    reminders: list[dict],
    gmail_activity: list[dict],
    linkedin_drafts: list[dict],
    issues: list,
    now: datetime,
) -> list[PictureItem]:
    items: list[PictureItem] = []

    for event in calendar_events[:5]:
        title = f"Calendar: {event.get('summary') or '(no title)'}"
        detail = _time_detail(event.get("start"))
        items.append(PictureItem("calendar", title, detail, _time_priority(event.get("start"), now, base=90)))

    for task in tasks[:6]:
        title = f"Task: {task.get('description')}"
        detail = f"due {task.get('due_date')}" if task.get("due_date") else "pending"
        items.append(PictureItem("tasks", title, detail, _date_priority(task.get("due_date"), now, base=80)))

    for reminder in reminders[:5]:
        title = f"Reminder: {reminder.get('message')}"
        detail = _time_detail(reminder.get("next_run_at"))
        items.append(PictureItem("reminders", title, detail, _time_priority(reminder.get("next_run_at"), now, base=75)))

    for row in gmail_activity[:4]:
        outcome = row.get("outcome") or "processed"
        title = f"Gmail: {row.get('subject') or '(no subject)'}"
        detail = f"{outcome} from {row.get('from') or 'unknown sender'}"
        priority = 65 if outcome in {"failed", "partial"} else 45
        items.append(PictureItem("gmail", title, detail, priority))

    for draft in linkedin_drafts[:4]:
        status = draft.get("status") or "queued"
        title = f"LinkedIn: {(draft.get('source_text') or draft.get('id', '')[:8])[:80]}"
        items.append(PictureItem("linkedin", title, status.replace("_", " "), 55 if status == "ready" else 48))

    for issue in issues[:8]:
        labels = {label.lower() for label in (issue.labels or [])}
        priority = 85 if "bug" in labels else 70 if labels.intersection({"feature", "enhancement"}) else 60
        label_text = f"[{', '.join(issue.labels)}]" if issue.labels else ""
        items.append(PictureItem("github", f"GitHub #{issue.number}: {issue.title}", label_text, priority))

    return items


def _rank_items(items: list[PictureItem]) -> list[PictureItem]:
    return sorted(items, key=lambda item: item.priority, reverse=True)


def _llm_order_top_three(items: list[PictureItem]) -> list[PictureItem]:
    if not settings.JARVIS_DAILY_PICTURE_LLM_RANKING or len(items) <= 3:
        return items
    try:
        payload = [
            {"index": index, "source": item.source, "title": item.title, "detail": item.detail, "priority": item.priority}
            for index, item in enumerate(items)
        ]
        ordered_indexes = generate_validated_json(
            task="digest",
            max_tokens=300,
            system=(
                "Rank daily operating picture items by practical urgency. "
                "Return JSON: {\"ordered_indexes\":[0,1,2]}. Use only provided indexes."
            ),
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            validator=lambda data: _validate_ranked_indexes(data, len(items)),
            allow_fallback=True,
        )
        seen: set[int] = set()
        ordered = []
        for index in ordered_indexes:
            if index not in seen:
                seen.add(index)
                ordered.append(items[index])
        ordered.extend(item for index, item in enumerate(items) if index not in seen)
        return ordered
    except Exception as exc:
        logger.debug("LLM ranking unavailable for daily picture: %s", exc)
        return items


def _validate_ranked_indexes(data: dict[str, Any], item_count: int) -> list[int]:
    raw = data.get("ordered_indexes")
    if not isinstance(raw, list):
        raise ValueError("ordered_indexes must be a list")
    indexes = []
    for value in raw:
        index = int(value)
        if 0 <= index < item_count:
            indexes.append(index)
    if not indexes:
        raise ValueError("ordered_indexes was empty")
    return indexes


def _format_calendar_section(events: list[dict], window_hours: int) -> list[str]:
    lines = [f"Calendar, next {window_hours}h:"]
    if not events:
        return lines + ["- No events found.", ""]
    for event in events[:5]:
        detail = _time_detail(event.get("start"))
        location = f" @ {event.get('location')}" if event.get("location") else ""
        lines.append(f"- {detail}: {event.get('summary') or '(no title)'}{location}")
    if len(events) > 5:
        lines.append(f"- ... and {len(events) - 5} more")
    lines.append("")
    return lines


def _format_task_section(tasks: list[dict]) -> list[str]:
    lines = ["Pending tasks:"]
    if not tasks:
        return lines + ["- None surfaced.", ""]
    for task in tasks[:5]:
        due = f" (due {task.get('due_date')})" if task.get("due_date") else ""
        lines.append(f"- {task.get('description')}{due}")
    if len(tasks) > 5:
        lines.append(f"- ... and {len(tasks) - 5} more")
    lines.append("")
    return lines


def _format_reminder_section(reminders: list[dict]) -> list[str]:
    lines = ["Active reminders:"]
    if not reminders:
        return lines + ["- None scheduled.", ""]
    for reminder in reminders[:5]:
        lines.append(f"- {_time_detail(reminder.get('next_run_at'))}: {reminder.get('message')}")
    if len(reminders) > 5:
        lines.append(f"- ... and {len(reminders) - 5} more")
    lines.append("")
    return lines


def _format_gmail_section(activity: list[dict]) -> list[str]:
    lines = ["Recent Gmail outcomes:"]
    if not activity:
        return lines + ["- No recent Gmail activity recorded.", ""]
    for row in activity[-5:]:
        lines.append(f"- {row.get('outcome', 'processed')}: {row.get('subject') or '(no subject)'}")
    lines.append("")
    return lines


def _format_linkedin_section(drafts: list[dict]) -> list[str]:
    lines = ["LinkedIn draft backlog:"]
    if not drafts:
        return lines + ["- Empty.", ""]
    counts: dict[str, int] = {}
    for draft in drafts:
        status = draft.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    lines.append("- " + ", ".join(f"{count} {status.replace('_', ' ')}" for status, count in sorted(counts.items())))
    for draft in drafts[:3]:
        lines.append(f"- {draft.get('id', '')[:8]} {draft.get('status')}: {(draft.get('source_text') or '')[:90]}")
    lines.append("")
    return lines


def _format_github_section(issues: list) -> list[str]:
    lines = ["Open GitHub issues:"]
    if not issues:
        return lines + ["- None found, or GitHub is not configured.", ""]
    for issue in issues[:5]:
        label_str = f" [{', '.join(issue.labels)}]" if issue.labels else ""
        lines.append(f"- #{issue.number} {issue.title}{label_str}")
    if len(issues) > 5:
        lines.append(f"- ... and {len(issues) - 5} more")
    lines.append("")
    return lines


def _time_detail(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "time unknown"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%H:%M")
    except ValueError:
        return text


def _time_priority(value: Any, now: datetime, *, base: int) -> int:
    text = str(value or "").strip()
    if not text:
        return base - 20
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None and now.tzinfo is not None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        hours = (parsed - now).total_seconds() / 3600
        if hours < 0:
            return base - 25
        if hours <= 2:
            return base + 10
        if hours <= 6:
            return base
    except ValueError:
        pass
    return base - 10


def _date_priority(value: Any, now: datetime, *, base: int) -> int:
    text = str(value or "").strip()
    if not text:
        return base - 20
    try:
        parsed = datetime.fromisoformat(text).date()
        days = (parsed - now.date()).days
        if days < 0:
            return base + 20
        if days == 0:
            return base + 15
        if days <= 2:
            return base
    except ValueError:
        pass
    return base - 10


def _seconds_until_next_morning(hour: int, minute: int, now: datetime | None = None) -> float:
    now = now or get_local_now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _parse_morning_time() -> tuple[int, int]:
    raw = settings.JARVIS_MORNING_TIME.strip()
    if raw:
        try:
            h, m = raw.split(":")
            return int(h), int(m)
        except (ValueError, AttributeError):
            logger.warning("Invalid JARVIS_MORNING_TIME '%s', defaulting to 09:00", raw)
    return 9, 0


class MorningDigestRunner:
    """Daemon-thread runner that sends one daily operating picture per day."""

    def __init__(
        self,
        notifier: "TelegramProactiveNotifier",
        *,
        memory_manager=None,
        reminder_manager=None,
        calendar_client=None,
    ) -> None:
        self._notifier = notifier
        self._memory = memory_manager
        self._reminders = reminder_manager
        self._calendar = calendar_client

    def run_forever(self) -> None:
        hour, minute = _parse_morning_time()
        logger.info("Daily operating picture scheduled for %02d:%02d local time", hour, minute)

        while True:
            sleep_secs = _seconds_until_next_morning(hour, minute)
            logger.debug("Daily operating picture sleeping %.0fs until next send", sleep_secs)
            time.sleep(sleep_secs)
            self._send()

    def _send(self) -> None:
        op_id = new_op_id("morning-digest")
        started = monotonic()
        with operation_context(op_id):
            try:
                message = build_daily_operating_picture(
                    memory_manager=self._memory,
                    reminder_manager=self._reminders,
                    calendar_client=self._calendar,
                )
                sent = self._notifier.send_message(message)
                duration_ms = (monotonic() - started) * 1000
                if sent:
                    record_activity(
                        event="morning_digest_sent",
                        component="morning_digest",
                        summary="Daily operating picture sent via Telegram",
                        duration_ms=duration_ms,
                    )
                    logger.info("Daily operating picture sent (%.0fms)", duration_ms)
                else:
                    record_issue(
                        level="WARNING",
                        event="morning_digest_send_failed",
                        component="morning_digest",
                        status="warning",
                        summary="Daily operating picture could not be delivered (notifier returned False)",
                        duration_ms=duration_ms,
                    )
            except Exception as exc:
                duration_ms = (monotonic() - started) * 1000
                logger.exception("Daily operating picture failed: %s", exc)
                record_issue(
                    level="ERROR",
                    event="morning_digest_error",
                    component="morning_digest",
                    status="error",
                    summary="Daily operating picture raised an unexpected error",
                    duration_ms=duration_ms,
                    metadata={"error": str(exc)},
                )
