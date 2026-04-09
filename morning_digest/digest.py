"""
Morning digest builder and scheduler.

Sends a daily morning Telegram message to Wess containing:
- A brief summary of open GitHub issues (tasks)
- The most promising backlog item to work on
- A one-sentence motivational note
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING

from config import settings
from core.opslog import new_op_id, operation_context, record_activity, record_issue
from core.time_utils import get_local_now

if TYPE_CHECKING:
    from telegram_bot.bot import TelegramProactiveNotifier

logger = logging.getLogger(__name__)

_MOTIVATIONAL_NOTES = [
    "Every line of code you write today is a step closer to the version of Marvis you're building.",
    "Ship something small, learn something new, and keep the momentum going.",
    "Focus beats busyness — one meaningful task done well is worth ten half-finished ones.",
    "The best time to fix a bug is right now, before it becomes someone else's emergency.",
    "Progress, not perfection — keep moving.",
    "Small consistent steps build great systems.",
    "Today's curiosity is tomorrow's feature.",
    "Code is communication — write it for the next person, even if that person is future you.",
    "Pick the one task that, if done today, makes everything else easier.",
    "Start with the hardest thing first; the rest of the day will feel like a gift.",
]


def _pick_motivational_note(now: datetime) -> str:
    """Pick a note deterministically from the day-of-year so it rotates daily."""
    day_of_year = now.timetuple().tm_yday
    return _MOTIVATIONAL_NOTES[day_of_year % len(_MOTIVATIONAL_NOTES)]


def _fetch_open_issues(limit: int = 10) -> list:
    """Fetch open GitHub issues. Returns empty list if GitHub is not configured."""
    try:
        from github_issues.client import GitHubIssuesClient, GitHubConfigError, load_github_client_config
        config = load_github_client_config()
        client = GitHubIssuesClient(config)
        return client.list_issues(state="open", limit=limit)
    except Exception as exc:
        logger.debug("GitHub issues unavailable for morning digest: %s", exc)
        return []


def _pick_top_backlog_item(issues: list) -> object | None:
    """Pick the single most actionable issue.

    Priority order:
    1. Issues labelled 'bug' (highest impact, unblocks work)
    2. Issues labelled 'feature' or 'enhancement'
    3. Most recently updated issue
    """
    if not issues:
        return None

    def _has_label(issue, *labels: str) -> bool:
        issue_labels = {l.lower() for l in (issue.labels or [])}
        return bool(issue_labels.intersection(labels))

    bugs = [i for i in issues if _has_label(i, "bug")]
    if bugs:
        return bugs[0]

    features = [i for i in issues if _has_label(i, "feature", "enhancement")]
    if features:
        return features[0]

    return issues[0]


def build_morning_message(now: datetime | None = None) -> str:
    """Compose the full morning digest text."""
    now = now or get_local_now()
    day_str = now.strftime("%A, %B %-d")

    lines: list[str] = [
        f"Good morning, Wess! Here's your Marvis digest for {day_str}.",
        "",
    ]

    issues = _fetch_open_issues(limit=10)

    if issues:
        lines.append(f"Open tasks ({len(issues)}):")
        for issue in issues[:5]:
            label_str = f" [{', '.join(issue.labels)}]" if issue.labels else ""
            lines.append(f"  #{issue.number} {issue.title}{label_str}")
        if len(issues) > 5:
            lines.append(f"  … and {len(issues) - 5} more")
        lines.append("")

        top = _pick_top_backlog_item(issues)
        if top:
            label_str = f" [{', '.join(top.labels)}]" if top.labels else ""
            lines.append(f"Most promising item to tackle: #{top.number} {top.title}{label_str}")
            lines.append("")
    else:
        lines.append("No open GitHub issues found — you're all clear, or GitHub isn't configured.")
        lines.append("")

    lines.append(_pick_motivational_note(now))

    return "\n".join(lines)


def _seconds_until_next_morning(hour: int, minute: int, now: datetime | None = None) -> float:
    """Return seconds until the next occurrence of HH:MM in local time."""
    now = now or get_local_now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _parse_morning_time() -> tuple[int, int]:
    """Parse JARVIS_MORNING_TIME (HH:MM) from settings. Defaults to 09:00."""
    raw = settings.JARVIS_MORNING_TIME.strip()
    if raw:
        try:
            h, m = raw.split(":")
            return int(h), int(m)
        except (ValueError, AttributeError):
            logger.warning("Invalid JARVIS_MORNING_TIME '%s', defaulting to 09:00", raw)
    return 9, 0


class MorningDigestRunner:
    """Daemon-thread runner that sends one morning digest per day."""

    def __init__(self, notifier: "TelegramProactiveNotifier") -> None:
        self._notifier = notifier

    def run_forever(self) -> None:
        hour, minute = _parse_morning_time()
        logger.info("Morning digest scheduled for %02d:%02d local time", hour, minute)

        while True:
            sleep_secs = _seconds_until_next_morning(hour, minute)
            logger.debug("Morning digest sleeping %.0fs until next send", sleep_secs)
            time.sleep(sleep_secs)

            self._send()

    def _send(self) -> None:
        op_id = new_op_id("morning-digest")
        started = monotonic()
        with operation_context(op_id):
            try:
                message = build_morning_message()
                sent = self._notifier.send_message(message)
                duration_ms = (monotonic() - started) * 1000
                if sent:
                    record_activity(
                        event="morning_digest_sent",
                        component="morning_digest",
                        summary="Daily morning digest sent via Telegram",
                        duration_ms=duration_ms,
                    )
                    logger.info("Morning digest sent (%.0fms)", duration_ms)
                else:
                    record_issue(
                        level="WARNING",
                        event="morning_digest_send_failed",
                        component="morning_digest",
                        status="warning",
                        summary="Morning digest could not be delivered (notifier returned False)",
                        duration_ms=duration_ms,
                    )
            except Exception as exc:
                duration_ms = (monotonic() - started) * 1000
                logger.exception("Morning digest failed: %s", exc)
                record_issue(
                    level="ERROR",
                    event="morning_digest_error",
                    component="morning_digest",
                    status="error",
                    summary="Morning digest raised an unexpected error",
                    duration_ms=duration_ms,
                    metadata={"error": str(exc)},
                )
