from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
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
        reminder = {
            "id": str(uuid.uuid4()),
            "message": cleaned_message,
            "scheduled_for": scheduled_utc.isoformat(),
            "next_run_at": scheduled_utc.isoformat(),
            "recurrence": normalized_recurrence,
            "status": "scheduled",
            "task_id": (task_id or "").strip() or None,
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
        now_iso = _utc_now(now).isoformat()
        sent_count = int(reminder.get("sent_count", 0)) + 1
        recurrence = reminder.get("recurrence")

        if recurrence:
            base_local = _from_db_datetime(reminder["next_run_at"]).astimezone(get_local_timezone())
            next_local = advance_recurrence(base_local, recurrence)
            next_run_at = _to_utc_iso(next_local)
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
        now_iso = _utc_now(now).isoformat()
        with self._lock:
            self._db.execute(
                """
                UPDATE reminders
                SET status='completed', completed_at=?, updated_at=?
                WHERE id=? AND status='scheduled'
                """,
                (now_iso, now_iso, reminder_id),
            )
            self._db.commit()
        return self.get_reminder(reminder_id)

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

                    sent = self._notifier.send_message(reminder["message"])
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
