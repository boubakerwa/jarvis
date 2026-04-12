from __future__ import annotations

import logging
import os
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Optional

from config import settings
from core.opslog import new_op_id, operation_context, record_activity, record_audit, record_issue
from core.time_utils import (
    advance_recurrence,
    describe_recurrence_rule,
    get_local_now,
    get_local_timezone,
    normalize_recurrence_rule,
    resolve_reminder_time,
)

if TYPE_CHECKING:
    from telegram_bot.bot import TelegramProactiveNotifier

logger = logging.getLogger(__name__)

_CREATE_REMINDERS_TABLE = """
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    message TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    next_run_at TEXT NOT NULL,
    recurrence TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    task_id TEXT,
    until_task_done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancelled_at TEXT,
    completed_at TEXT,
    last_sent_at TEXT,
    last_error TEXT,
    sent_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status);
CREATE INDEX IF NOT EXISTS idx_reminders_next_run_at ON reminders(next_run_at);
CREATE INDEX IF NOT EXISTS idx_reminders_task_id ON reminders(task_id);
"""

_CREATE_CHAT_RESET_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS chat_reset_sessions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    started_at TEXT NOT NULL,
    next_run_at TEXT NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    last_sent_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    dismissed_at TEXT,
    reset_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_reset_sessions_status ON chat_reset_sessions(status);
CREATE INDEX IF NOT EXISTS idx_chat_reset_sessions_next_run_at ON chat_reset_sessions(next_run_at);
"""

_FOLLOW_UP_INTERVALS = (
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=4),
    timedelta(days=1),
)

_FOLLOW_UP_STAGE_KEYS = ("5m", "30m", "1h", "4h", "daily")
_FOLLOW_UP_STAGE_MESSAGES: dict[str, tuple[str, ...]] = {
    "5m": (
        "Five minutes later, and this is still sitting here: {task}",
        "Tiny follow-up: {task} is still waiting and pretending that is normal.",
        "Quick reality check: {task}",
        "The clock moved. The task did not: {task}",
        "Short snooze over. Back to business: {task}",
        "Friendly harassment round one: {task}",
        "This is your five-minute audit. Item under review: {task}",
        "Mini nudge, maximum judgment: {task}",
        "That was a very short break. Anyway: {task}",
        "The task has reappeared like a tax form: {task}",
    ),
    "30m": (
        "Thirty minutes later, this is officially becoming a choice: {task}",
        "Half an hour has passed. Bold strategy. Anyway: {task}",
        "Escalation level one: {task}",
        "This task has now survived one respectable snooze: {task}",
        "At this point the reminder has more discipline than the task: {task}",
        "Thirty-minute review board says: still not done. Item: {task}",
        "Small problem becoming medium problem: {task}",
        "This has now been ignored long enough to earn a sequel: {task}",
        "Half-hour checkpoint. The situation remains suspicious: {task}",
        "We are no longer in the 'forgot' phase. We are in the 'avoided' phase: {task}",
    ),
    "1h": (
        "One hour later, and this has entered comedy territory: {task}",
        "Hourly inspection: {task} is still hanging around.",
        "This is now an established subplot: {task}",
        "One full hour. The reminder would like a word: {task}",
        "At this pace the task may outlive us both: {task}",
        "Respectfully, this has become ridiculous: {task}",
        "Hourly nudge with light disappointment attached: {task}",
        "An hour later, and the file on this case is getting thicker: {task}",
        "Status report: still unresolved, increasingly dramatic. Item: {task}",
        "This reminder has now clocked in for a full shift: {task}",
    ),
    "4h": (
        "Four hours later, we need to have a serious conversation: {task}",
        "This has been pending long enough to develop character: {task}",
        "Four-hour escalation. Morale is declining. Task: {task}",
        "The reminder would like to note a pattern: {task}",
        "At this point the task is basically renting space in the day: {task}",
        "Four hours in, and the joke is becoming documentary footage: {task}",
        "This reminder has seen things. Chief among them: {task}",
        "Operational concern: {task} remains very much alive.",
        "Four-hour check-in from the Department of Unfinished Business: {task}",
        "This is no longer a nudge. It is an intervention: {task}",
    ),
    "daily": (
        "Daily reminder: somehow this still exists. Task: {task}",
        "A new day, the same unfinished legend: {task}",
        "Good morning. Bad news. We are still tracking: {task}",
        "Daily audit: {task} has once again passed into another day untouched.",
        "This task is now on the long-term residency program: {task}",
        "Another day has arrived. So has this reminder. Item: {task}",
        "Daily escalation note: the empire of procrastination still stands. Subject: {task}",
        "This has achieved recurring-character status: {task}",
        "Fresh day, stale task: {task}",
        "The sun came back. So did I. Task still pending: {task}",
    ),
}

_CHAT_RESET_INITIAL_OFFSETS_MINUTES = (3, 10, 15)
_CHAT_RESET_REPEAT_INTERVAL_MINUTES = 5
_CHAT_RESET_MESSAGES: tuple[str, ...] = (
    "This chat has been open for 3 minutes. If we are switching topics, reset it before context turns into soup.",
    "10-minute checkpoint. If this is drifting into a new subject, reset the chat and spare future-you the confusion.",
    "15 minutes in. The thread is now old enough to start lying by omission. Reset if we are pivoting.",
    "Still going. If we are mixing topics, reset the chat. If not, dismiss me and live with your choices.",
)


def _utc_now(now: Optional[datetime] = None) -> datetime:
    base = get_local_now(now)
    return base.astimezone(timezone.utc)


def _to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Reminder times must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _from_db_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_local_datetime(value: str) -> str:
    local = _from_db_datetime(value).astimezone(get_local_timezone())
    return local.strftime("%Y-%m-%d %H:%M %Z")


class ReminderManager:
    def __init__(self, db_path: str | None = None):
        path = db_path or settings.JARVIS_DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_CREATE_REMINDERS_TABLE)
        self._ensure_tasks_table()
        self._db.commit()
        self._lock = threading.Lock()

    def schedule_message(
        self,
        message: str,
        when: str,
        *,
        recurrence: str | None = None,
        task_id: str | None = None,
        until_task_done: bool = False,
        now: datetime | None = None,
    ) -> dict:
        cleaned_message = (message or "").strip()
        if not cleaned_message:
            raise ValueError("Reminder message cannot be empty")

        scheduled_local = resolve_reminder_time(when, now=now)
        current_utc = _utc_now(now)
        scheduled_utc = scheduled_local.astimezone(timezone.utc)
        if scheduled_utc <= current_utc:
            raise ValueError("Reminder time must be in the future")

        normalized_recurrence = normalize_recurrence_rule(recurrence)
        now_iso = current_utc.isoformat()
        linked_task_id = (task_id or "").strip() or None
        with self._lock:
            if linked_task_id is None:
                linked_task_id = self._create_backing_task(cleaned_message, scheduled_utc.isoformat(), now_iso=now_iso)
        reminder = {
            "id": str(uuid.uuid4()),
            "message": cleaned_message,
            "scheduled_for": scheduled_utc.isoformat(),
            "next_run_at": scheduled_utc.isoformat(),
            "recurrence": normalized_recurrence,
            "status": "scheduled",
            "task_id": linked_task_id,
            "until_task_done": 1 if until_task_done else 0,
            "created_at": now_iso,
            "updated_at": now_iso,
            "cancelled_at": None,
            "completed_at": None,
            "last_sent_at": None,
            "last_error": None,
            "sent_count": 0,
        }

        with self._lock:
            self._db.execute(
                """
                INSERT INTO reminders (
                    id, message, scheduled_for, next_run_at, recurrence, status,
                    task_id, until_task_done, created_at, updated_at,
                    cancelled_at, completed_at, last_sent_at, last_error, sent_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder["id"],
                    reminder["message"],
                    reminder["scheduled_for"],
                    reminder["next_run_at"],
                    reminder["recurrence"],
                    reminder["status"],
                    reminder["task_id"],
                    reminder["until_task_done"],
                    reminder["created_at"],
                    reminder["updated_at"],
                    reminder["cancelled_at"],
                    reminder["completed_at"],
                    reminder["last_sent_at"],
                    reminder["last_error"],
                    reminder["sent_count"],
                ),
            )
            self._db.commit()

        record_audit(
            event="reminder_scheduled",
            component="reminders",
            summary="Scheduled Telegram reminder",
            metadata={
                "reminder_id": reminder["id"],
                "scheduled_for": reminder["scheduled_for"],
                "recurrence": reminder["recurrence"] or "",
            },
        )
        return reminder

    def list_reminders(self, status: str = "scheduled") -> list[dict]:
        allowed_statuses = {"scheduled", "cancelled", "completed", "all"}
        if status not in allowed_statuses:
            raise ValueError(f"Unsupported reminder status: {status}")

        with self._lock:
            if status == "all":
                rows = self._db.execute(
                    "SELECT * FROM reminders ORDER BY next_run_at, created_at"
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM reminders WHERE status=? ORDER BY next_run_at, created_at",
                    (status,),
                ).fetchall()
        return [dict(row) for row in rows]

    def cancel_reminder(self, reminder_id: str, *, now: datetime | None = None) -> dict | None:
        resolved_id = self._resolve_id(reminder_id, statuses=("scheduled",))
        if resolved_id is None:
            return None

        now_iso = _utc_now(now).isoformat()
        with self._lock:
            self._db.execute(
                """
                UPDATE reminders
                SET status='cancelled', cancelled_at=?, updated_at=?
                WHERE id=? AND status='scheduled'
                """,
                (now_iso, now_iso, resolved_id),
            )
            self._db.commit()
        return self.get_reminder(resolved_id)

    def get_reminder(self, reminder_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM reminders WHERE id=?",
                (reminder_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_due_reminders(self, *, now: datetime | None = None, limit: int = 20) -> list[dict]:
        now_iso = _utc_now(now).isoformat()
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM reminders
                WHERE status='scheduled' AND next_run_at <= ?
                ORDER BY next_run_at, created_at
                LIMIT ?
                """,
                (now_iso, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_sent(self, reminder: dict, *, now: datetime | None = None) -> dict:
        reminder_id = reminder["id"]
        current_utc = _utc_now(now)
        now_iso = current_utc.isoformat()
        sent_count = int(reminder.get("sent_count", 0)) + 1
        recurrence = reminder.get("recurrence")

        if recurrence:
            base_local = _from_db_datetime(reminder["next_run_at"]).astimezone(get_local_timezone())
            next_local = advance_recurrence(base_local, recurrence)
            next_run_at = _to_utc_iso(next_local)
            status = "scheduled"
            completed_at = None
        elif reminder.get("until_task_done"):
            next_run_at = (current_utc + _follow_up_interval(sent_count)).isoformat()
            status = "scheduled"
            completed_at = None
        else:
            next_run_at = reminder["next_run_at"]
            status = "completed"
            completed_at = now_iso

        with self._lock:
            self._db.execute(
                """
                UPDATE reminders
                SET status=?, next_run_at=?, updated_at=?, completed_at=?,
                    last_sent_at=?, last_error=NULL, sent_count=?
                WHERE id=?
                """,
                (
                    status,
                    next_run_at,
                    now_iso,
                    completed_at,
                    now_iso,
                    sent_count,
                    reminder_id,
                ),
            )
            self._db.commit()
        return self.get_reminder(reminder_id) or reminder

    def snooze_reminder(self, reminder_id: str, *, now: datetime | None = None) -> dict | None:
        reminder = self._get_actionable_reminder(reminder_id)
        if reminder is None:
            return None

        current_utc = _utc_now(now)
        next_run_at = (current_utc + _follow_up_interval(int(reminder.get("sent_count", 0)) + 1)).isoformat()
        now_iso = current_utc.isoformat()
        with self._lock:
            self._db.execute(
                """
                UPDATE reminders
                SET status='scheduled', next_run_at=?, updated_at=?, completed_at=NULL, cancelled_at=NULL
                WHERE id=?
                """,
                (next_run_at, now_iso, reminder["id"]),
            )
            self._db.commit()
        return self.get_reminder(reminder["id"])

    def mark_delivery_failed(self, reminder_id: str, error: str, *, now: datetime | None = None) -> None:
        now_iso = _utc_now(now).isoformat()
        with self._lock:
            self._db.execute(
                "UPDATE reminders SET updated_at=?, last_error=? WHERE id=?",
                (now_iso, error, reminder_id),
            )
            self._db.commit()

    def complete_linked_reminders(self, task_id: str, *, now: datetime | None = None) -> int:
        now_iso = _utc_now(now).isoformat()
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE reminders
                SET status='completed', completed_at=?, updated_at=?
                WHERE task_id=? AND until_task_done=1 AND status='scheduled'
                """,
                (now_iso, now_iso, task_id),
            )
            self._db.commit()
        return cursor.rowcount

    def is_task_done(self, task_id: str) -> bool:
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT status FROM tasks WHERE id=?",
                    (task_id,),
                ).fetchone()
        except sqlite3.OperationalError:
            return False
        return bool(row and row["status"] == "done")

    def mark_completed(self, reminder_id: str, *, now: datetime | None = None) -> dict | None:
        reminder = self._get_actionable_reminder(reminder_id)
        if reminder is None:
            return None
        now_iso = _utc_now(now).isoformat()
        with self._lock:
            self._db.execute(
                """
                UPDATE reminders
                SET status='completed', completed_at=?, updated_at=?
                WHERE id=? AND status IN ('scheduled', 'completed')
                """,
                (now_iso, now_iso, reminder["id"]),
            )
            self._db.commit()
        return self.get_reminder(reminder["id"])

    def describe_reminder(self, reminder: dict) -> str:
        short_id = reminder["id"][:8]
        repeat_text = describe_recurrence_rule(reminder.get("recurrence"))
        status = reminder.get("status", "scheduled")
        task_suffix = ""
        if reminder.get("task_id"):
            task_suffix = f", task {str(reminder['task_id'])[:8]}"
        return (
            f"[{short_id}] {status} for {_format_local_datetime(reminder['next_run_at'])}"
            f" ({repeat_text}{task_suffix}) — {reminder['message']}"
        )

    def _resolve_id(self, prefix: str, *, statuses: tuple[str, ...]) -> str | None:
        cleaned = (prefix or "").strip()
        if not cleaned:
            return None

        with self._lock:
            exact = self._db.execute(
                f"SELECT id FROM reminders WHERE id=? AND status IN ({','.join('?' for _ in statuses)})",
                (cleaned, *statuses),
            ).fetchall()
            if exact:
                return exact[0]["id"]

            matches = self._db.execute(
                f"SELECT id FROM reminders WHERE id LIKE ? AND status IN ({','.join('?' for _ in statuses)}) ORDER BY created_at",
                (f"{cleaned}%", *statuses),
            ).fetchall()

        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            raise ValueError(f"Reminder ID '{prefix}' is ambiguous")
        return None

    def _get_actionable_reminder(self, reminder_id: str) -> dict | None:
        resolved_id = self._resolve_id(reminder_id, statuses=("scheduled", "completed"))
        if resolved_id is None:
            return None
        return self.get_reminder(resolved_id)

    def _create_backing_task(self, description: str, due_date: str, *, now_iso: str) -> str:
        task_id = str(uuid.uuid4())
        self._db.execute(
            """
            INSERT INTO tasks (id, description, due_date, status, source, surfaced, created_at, completed_at)
            VALUES (?, ?, ?, 'pending', 'reminder', 0, ?, NULL)
            """,
            (task_id, description, due_date, now_iso),
        )
        self._db.commit()
        return task_id

    def _ensure_tasks_table(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                due_date TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT NOT NULL DEFAULT 'manual',
                surfaced INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        columns = {
            row["name"] for row in self._db.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "source" not in columns:
            self._db.execute("ALTER TABLE tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        if "surfaced" not in columns:
            self._db.execute("ALTER TABLE tasks ADD COLUMN surfaced INTEGER NOT NULL DEFAULT 1")


class ChatResetSessionManager:
    def __init__(self, db_path: str | None = None):
        path = db_path or settings.JARVIS_DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_CREATE_CHAT_RESET_SESSIONS_TABLE)
        self._db.commit()
        self._lock = threading.Lock()

    def start_session(self, *, now: datetime | None = None, force_new: bool = False) -> dict:
        current_utc = _utc_now(now)
        now_iso = current_utc.isoformat()
        with self._lock:
            if force_new:
                self._db.execute(
                    """
                    UPDATE chat_reset_sessions
                    SET status='reset', reset_at=?, updated_at=?
                    WHERE status IN ('active', 'dismissed')
                    """,
                    (now_iso, now_iso),
                )
            else:
                existing = self._get_open_session_locked()
                if existing is not None:
                    return existing

            session = {
                "id": str(uuid.uuid4()),
                "status": "active",
                "started_at": now_iso,
                "next_run_at": (current_utc + _chat_reset_offset_for_delivery(1)).isoformat(),
                "sent_count": 0,
                "last_sent_at": None,
                "last_error": None,
                "created_at": now_iso,
                "updated_at": now_iso,
                "dismissed_at": None,
                "reset_at": None,
            }
            self._db.execute(
                """
                INSERT INTO chat_reset_sessions (
                    id, status, started_at, next_run_at, sent_count, last_sent_at,
                    last_error, created_at, updated_at, dismissed_at, reset_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["id"],
                    session["status"],
                    session["started_at"],
                    session["next_run_at"],
                    session["sent_count"],
                    session["last_sent_at"],
                    session["last_error"],
                    session["created_at"],
                    session["updated_at"],
                    session["dismissed_at"],
                    session["reset_at"],
                ),
            )
            self._db.commit()
        record_audit(
            event="chat_reset_session_started",
            component="telegram",
            summary="Started chat-reset reminder session",
            metadata={"session_id": session["id"]},
        )
        return session

    def get_due_sessions(self, *, now: datetime | None = None, limit: int = 20) -> list[dict]:
        now_iso = _utc_now(now).isoformat()
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM chat_reset_sessions
                WHERE status='active' AND next_run_at <= ?
                ORDER BY next_run_at, created_at
                LIMIT ?
                """,
                (now_iso, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_sent(self, session: dict, *, now: datetime | None = None) -> dict:
        current_utc = _utc_now(now)
        now_iso = current_utc.isoformat()
        sent_count = int(session.get("sent_count", 0)) + 1
        started_at = _from_db_datetime(session["started_at"])
        next_delivery_number = sent_count + 1
        next_run_at = (started_at + _chat_reset_offset_for_delivery(next_delivery_number)).isoformat()

        with self._lock:
            self._db.execute(
                """
                UPDATE chat_reset_sessions
                SET sent_count=?, last_sent_at=?, next_run_at=?, updated_at=?, last_error=NULL
                WHERE id=? AND status='active'
                """,
                (sent_count, now_iso, next_run_at, now_iso, session["id"]),
            )
            self._db.commit()
        return self.get_session(session["id"]) or session

    def dismiss_session(self, session_id: str | None = None, *, now: datetime | None = None) -> dict | None:
        return self._transition_session(session_id, "dismissed", now=now)

    def reset_session(self, session_id: str | None = None, *, now: datetime | None = None) -> dict | None:
        return self._transition_session(session_id, "reset", now=now)

    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM chat_reset_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_current_session(self) -> dict | None:
        with self._lock:
            return self._get_open_session_locked()

    def mark_delivery_failed(self, session_id: str, error: str, *, now: datetime | None = None) -> None:
        now_iso = _utc_now(now).isoformat()
        with self._lock:
            self._db.execute(
                "UPDATE chat_reset_sessions SET updated_at=?, last_error=? WHERE id=?",
                (now_iso, error, session_id),
            )
            self._db.commit()

    def _transition_session(self, session_id: str | None, target_status: str, *, now: datetime | None = None) -> dict | None:
        current_utc = _utc_now(now)
        now_iso = current_utc.isoformat()
        with self._lock:
            session = self._resolve_session_locked(session_id)
            if session is None:
                return None
            field = "dismissed_at" if target_status == "dismissed" else "reset_at"
            self._db.execute(
                f"""
                UPDATE chat_reset_sessions
                SET status=?, {field}=?, updated_at=?
                WHERE id=? AND status IN ('active', 'dismissed')
                """,
                (target_status, now_iso, now_iso, session["id"]),
            )
            self._db.commit()
        return self.get_session(session["id"])

    def _resolve_session_locked(self, session_id: str | None) -> dict | None:
        if session_id:
            row = self._db.execute(
                "SELECT * FROM chat_reset_sessions WHERE id=? AND status IN ('active', 'dismissed')",
                (session_id,),
            ).fetchone()
            if row:
                return dict(row)
        return self._get_open_session_locked()

    def _get_open_session_locked(self) -> dict | None:
        row = self._db.execute(
            """
            SELECT * FROM chat_reset_sessions
            WHERE status IN ('active', 'dismissed')
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None


class ReminderDeliveryRunner:
    def __init__(
        self,
        *,
        reminder_manager: ReminderManager,
        notifier: "TelegramProactiveNotifier",
        poll_interval_seconds: int = 30,
    ) -> None:
        self._reminders = reminder_manager
        self._notifier = notifier
        self._poll_interval_seconds = max(1, poll_interval_seconds)

    def run_forever(self) -> None:
        logger.info("Reminder delivery loop started (interval: %ss)", self._poll_interval_seconds)
        while True:
            self.run_once()
            time.sleep(self._poll_interval_seconds)

    def run_once(self, *, now: datetime | None = None) -> int:
        reminders = self._reminders.get_due_reminders(now=now)
        delivered = 0

        for reminder in reminders:
            op_id = new_op_id("reminder")
            started = monotonic()
            with operation_context(op_id):
                try:
                    if reminder.get("until_task_done") and reminder.get("task_id") and self._reminders.is_task_done(reminder["task_id"]):
                        self._reminders.mark_completed(reminder["id"], now=now)
                        record_activity(
                            event="reminder_completed_without_send",
                            component="reminders",
                            summary="Reminder stopped because linked task is already done",
                            duration_ms=(monotonic() - started) * 1000,
                            metadata={"reminder_id": reminder["id"], "task_id": reminder["task_id"]},
                        )
                        continue

                    delivery_text, delivery_stage = _build_reminder_delivery_text(reminder)
                    sent = self._notifier.send_message(
                        delivery_text,
                        reply_markup=_build_reminder_reply_markup(reminder["id"]),
                    )
                    duration_ms = (monotonic() - started) * 1000
                    if sent:
                        updated = self._reminders.mark_sent(reminder, now=now)
                        delivered += 1
                        record_activity(
                            event="reminder_sent",
                            component="reminders",
                            summary="Scheduled reminder sent via Telegram",
                            duration_ms=duration_ms,
                            metadata={
                                "reminder_id": reminder["id"],
                                "recurrence": updated.get("recurrence") or "",
                                "next_run_at": updated.get("next_run_at", ""),
                                "delivery_stage": delivery_stage or "initial",
                            },
                        )
                    else:
                        error = "Notifier returned False"
                        self._reminders.mark_delivery_failed(reminder["id"], error, now=now)
                        record_issue(
                            level="WARNING",
                            event="reminder_send_failed",
                            component="reminders",
                            status="warning",
                            summary="Scheduled reminder could not be delivered",
                            duration_ms=duration_ms,
                            metadata={"reminder_id": reminder["id"], "error": error},
                        )
                except Exception as exc:
                    self._reminders.mark_delivery_failed(reminder["id"], str(exc), now=now)
                    logger.exception("Reminder delivery failed: %s", exc)
                    record_issue(
                        level="ERROR",
                        event="reminder_delivery_error",
                        component="reminders",
                        status="error",
                        summary="Scheduled reminder raised an unexpected error",
                        duration_ms=(monotonic() - started) * 1000,
                        metadata={"reminder_id": reminder["id"], "error": str(exc)},
                    )

        return delivered


class ChatResetDeliveryRunner:
    def __init__(
        self,
        *,
        session_manager: ChatResetSessionManager,
        notifier: "TelegramProactiveNotifier",
        poll_interval_seconds: int = 30,
    ) -> None:
        self._sessions = session_manager
        self._notifier = notifier
        self._poll_interval_seconds = max(1, poll_interval_seconds)

    def run_forever(self) -> None:
        logger.info("Chat-reset delivery loop started (interval: %ss)", self._poll_interval_seconds)
        while True:
            self.run_once()
            time.sleep(self._poll_interval_seconds)

    def run_once(self, *, now: datetime | None = None) -> int:
        sessions = self._sessions.get_due_sessions(now=now)
        delivered = 0

        for session in sessions:
            op_id = new_op_id("chat-reset")
            started = monotonic()
            with operation_context(op_id):
                try:
                    sent = self._notifier.send_message(
                        _build_chat_reset_delivery_text(session),
                        reply_markup=_build_chat_reset_reply_markup(session["id"]),
                    )
                    duration_ms = (monotonic() - started) * 1000
                    if sent:
                        updated = self._sessions.mark_sent(session, now=now)
                        delivered += 1
                        record_activity(
                            event="chat_reset_reminder_sent",
                            component="telegram",
                            summary="Sent scheduled chat-reset reminder",
                            duration_ms=duration_ms,
                            metadata={
                                "session_id": session["id"],
                                "sent_count": updated.get("sent_count", 0),
                                "next_run_at": updated.get("next_run_at", ""),
                            },
                        )
                    else:
                        error = "Notifier returned False"
                        self._sessions.mark_delivery_failed(session["id"], error, now=now)
                        record_issue(
                            level="WARNING",
                            event="chat_reset_reminder_send_failed",
                            component="telegram",
                            status="warning",
                            summary="Chat-reset reminder could not be delivered",
                            duration_ms=duration_ms,
                            metadata={"session_id": session["id"], "error": error},
                        )
                except Exception as exc:
                    self._sessions.mark_delivery_failed(session["id"], str(exc), now=now)
                    logger.exception("Chat-reset reminder delivery failed: %s", exc)
                    record_issue(
                        level="ERROR",
                        event="chat_reset_reminder_delivery_error",
                        component="telegram",
                        status="error",
                        summary="Chat-reset reminder raised an unexpected error",
                        duration_ms=(monotonic() - started) * 1000,
                        metadata={"session_id": session["id"], "error": str(exc)},
                    )

        return delivered


def _follow_up_interval(sent_count: int) -> timedelta:
    index = max(sent_count - 1, 0)
    if index >= len(_FOLLOW_UP_INTERVALS):
        return _FOLLOW_UP_INTERVALS[-1]
    return _FOLLOW_UP_INTERVALS[index]


def _build_reminder_delivery_text(reminder: dict) -> tuple[str, str | None]:
    task = str(reminder.get("message") or "").strip()
    sent_count = int(reminder.get("sent_count", 0))
    if sent_count <= 0:
        return task, None

    stage_index = min(sent_count - 1, len(_FOLLOW_UP_STAGE_KEYS) - 1)
    stage_key = _FOLLOW_UP_STAGE_KEYS[stage_index]
    template = random.choice(_FOLLOW_UP_STAGE_MESSAGES[stage_key])
    return template.format(task=task), stage_key


def _build_reminder_reply_markup(reminder_id: str):
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:
        return None

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Done", callback_data=f"reminder:done:{reminder_id}"),
                InlineKeyboardButton("Remind me later", callback_data=f"reminder:later:{reminder_id}"),
            ]
        ]
    )


def _chat_reset_offset_for_delivery(delivery_number: int) -> timedelta:
    index = max(delivery_number, 1)
    if index <= len(_CHAT_RESET_INITIAL_OFFSETS_MINUTES):
        return timedelta(minutes=_CHAT_RESET_INITIAL_OFFSETS_MINUTES[index - 1])
    return timedelta(
        minutes=_CHAT_RESET_INITIAL_OFFSETS_MINUTES[-1]
        + _CHAT_RESET_REPEAT_INTERVAL_MINUTES * (index - len(_CHAT_RESET_INITIAL_OFFSETS_MINUTES))
    )


def _build_chat_reset_delivery_text(session: dict) -> str:
    sent_count = int(session.get("sent_count", 0))
    if sent_count < 0:
        sent_count = 0
    index = min(sent_count, len(_CHAT_RESET_MESSAGES) - 1)
    return _CHAT_RESET_MESSAGES[index]


def _build_chat_reset_reply_markup(session_id: str):
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:
        return None

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Reset Chat", callback_data=f"chatreset:reset:{session_id}"),
                InlineKeyboardButton("Dismiss", callback_data=f"chatreset:dismiss:{session_id}"),
            ]
        ]
    )
