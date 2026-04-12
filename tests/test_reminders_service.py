import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeNotifier:
    def __init__(self, should_send: bool = True):
        self.should_send = should_send
        self.messages = []

    def send_message(self, text: str, *, reply_markup=None) -> bool:
        self.messages.append((text, reply_markup))
        return self.should_send


class ReminderServiceTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module("tested_reminders_service", "reminders/service.py")
        self.now = datetime(2026, 4, 5, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    def test_schedule_and_cancel_reminder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self.module.ReminderManager(db_path=str(Path(tmpdir) / "jarvis.db"))

            reminder = manager.schedule_message(
                "Call the doctor",
                "tomorrow at 10am",
                now=self.now,
            )

            self.assertEqual(reminder["status"], "scheduled")
            reminders = manager.list_reminders()
            self.assertEqual(len(reminders), 1)
            self.assertEqual(reminders[0]["message"], "Call the doctor")

            cancelled = manager.cancel_reminder(reminder["id"][:8], now=self.now)
            self.assertIsNotNone(cancelled)
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertTrue(reminder["task_id"])

    def test_schedule_message_creates_hidden_backing_task_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self.module.ReminderManager(db_path=str(Path(tmpdir) / "jarvis.db"))

            reminder = manager.schedule_message(
                "Call the doctor",
                "tomorrow at 10am",
                now=self.now,
            )

            row = manager._db.execute(
                "SELECT description, due_date, status, source, surfaced FROM tasks WHERE id=?",
                (reminder["task_id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["description"], "Call the doctor")
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["source"], "reminder")
            self.assertEqual(row["surfaced"], 0)

    def test_runner_sends_one_off_reminder_and_marks_completed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self.module.ReminderManager(db_path=str(Path(tmpdir) / "jarvis.db"))
            reminder = manager.schedule_message(
                "Ping Wess",
                "2026-04-05T12:05:00+02:00",
                now=self.now,
            )
            notifier = FakeNotifier()
            runner = self.module.ReminderDeliveryRunner(
                reminder_manager=manager,
                notifier=notifier,
                poll_interval_seconds=1,
            )

            delivered = runner.run_once(now=datetime(2026, 4, 5, 12, 6, tzinfo=ZoneInfo("Europe/Berlin")))

            self.assertEqual(delivered, 1)
            self.assertEqual(notifier.messages[0][0], "Ping Wess")
            updated = manager.get_reminder(reminder["id"])
            self.assertIsNotNone(updated)
            self.assertEqual(updated["status"], "completed")
            self.assertEqual(updated["sent_count"], 1)

    def test_runner_reschedules_recurring_reminder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self.module.ReminderManager(db_path=str(Path(tmpdir) / "jarvis.db"))
            reminder = manager.schedule_message(
                "Stand up and stretch",
                "2026-04-05T12:05:00+02:00",
                recurrence="daily",
                now=self.now,
            )
            notifier = FakeNotifier()
            runner = self.module.ReminderDeliveryRunner(
                reminder_manager=manager,
                notifier=notifier,
                poll_interval_seconds=1,
            )

            runner.run_once(now=datetime(2026, 4, 5, 12, 6, tzinfo=ZoneInfo("Europe/Berlin")))

            updated = manager.get_reminder(reminder["id"])
            self.assertIsNotNone(updated)
            self.assertEqual(updated["status"], "scheduled")
            self.assertEqual(updated["sent_count"], 1)
            self.assertEqual(updated["next_run_at"], "2026-04-06T10:05:00+00:00")

    def test_runner_skips_until_done_reminder_when_task_is_already_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "jarvis.db"
            manager = self.module.ReminderManager(db_path=str(db_path))
            manager._db.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    due_date TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            manager._db.execute(
                """
                INSERT INTO tasks (id, description, status, created_at, completed_at)
                VALUES (?, ?, 'done', ?, ?)
                """,
                ("task-1234", "Finish taxes", "2026-04-05T09:00:00+00:00", "2026-04-05T10:00:00+00:00"),
            )
            manager._db.commit()

            reminder = manager.schedule_message(
                "Finish taxes",
                "2026-04-05T12:05:00+02:00",
                recurrence="daily",
                task_id="task-1234",
                until_task_done=True,
                now=self.now,
            )
            notifier = FakeNotifier()
            runner = self.module.ReminderDeliveryRunner(
                reminder_manager=manager,
                notifier=notifier,
                poll_interval_seconds=1,
            )

            delivered = runner.run_once(now=datetime(2026, 4, 5, 12, 6, tzinfo=ZoneInfo("Europe/Berlin")))

            self.assertEqual(delivered, 0)
            self.assertEqual(notifier.messages, [])
            updated = manager.get_reminder(reminder["id"])
            self.assertIsNotNone(updated)
            self.assertEqual(updated["status"], "completed")

    def test_runner_reschedules_until_done_reminder_with_escalating_follow_up(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self.module.ReminderManager(db_path=str(Path(tmpdir) / "jarvis.db"))
            reminder = manager.schedule_message(
                "Finish taxes",
                "2026-04-05T12:05:00+02:00",
                task_id="task-1234",
                until_task_done=True,
                now=self.now,
            )
            notifier = FakeNotifier()
            runner = self.module.ReminderDeliveryRunner(
                reminder_manager=manager,
                notifier=notifier,
                poll_interval_seconds=1,
            )

            runner.run_once(now=datetime(2026, 4, 5, 12, 6, tzinfo=ZoneInfo("Europe/Berlin")))

            updated = manager.get_reminder(reminder["id"])
            self.assertIsNotNone(updated)
            self.assertEqual(updated["status"], "scheduled")
            self.assertEqual(updated["sent_count"], 1)
            self.assertEqual(updated["next_run_at"], "2026-04-05T10:11:00+00:00")

    def test_snooze_reminder_reopens_completed_one_off(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self.module.ReminderManager(db_path=str(Path(tmpdir) / "jarvis.db"))
            reminder = manager.schedule_message(
                "Ping Wess",
                "2026-04-05T12:05:00+02:00",
                now=self.now,
            )
            manager.mark_sent(reminder, now=datetime(2026, 4, 5, 12, 6, tzinfo=ZoneInfo("Europe/Berlin")))

            snoozed = manager.snooze_reminder(
                reminder["id"][:8],
                now=datetime(2026, 4, 5, 12, 10, tzinfo=ZoneInfo("Europe/Berlin")),
            )

            self.assertIsNotNone(snoozed)
            assert snoozed is not None
            self.assertEqual(snoozed["status"], "scheduled")
            self.assertEqual(snoozed["next_run_at"], "2026-04-05T10:40:00+00:00")

    def test_follow_up_reminder_uses_random_stage_message_without_llm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self.module.ReminderManager(db_path=str(Path(tmpdir) / "jarvis.db"))
            reminder = manager.schedule_message(
                "Finish taxes",
                "2026-04-05T12:05:00+02:00",
                task_id="task-1234",
                until_task_done=True,
                now=self.now,
            )
            notifier = FakeNotifier()
            runner = self.module.ReminderDeliveryRunner(
                reminder_manager=manager,
                notifier=notifier,
                poll_interval_seconds=1,
            )

            runner.run_once(now=datetime(2026, 4, 5, 12, 6, tzinfo=ZoneInfo("Europe/Berlin")))
            self.assertEqual(notifier.messages[0][0], "Finish taxes")

            with patch.object(self.module.random, "choice", return_value="Escalation test: {task}"):
                runner.run_once(now=datetime(2026, 4, 5, 12, 12, tzinfo=ZoneInfo("Europe/Berlin")))

            self.assertEqual(notifier.messages[1][0], "Escalation test: Finish taxes")

    def test_chat_reset_session_uses_absolute_schedule_offsets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self.module.ChatResetSessionManager(db_path=str(Path(tmpdir) / "jarvis.db"))

            session = manager.start_session(now=self.now, force_new=True)
            self.assertEqual(session["next_run_at"], "2026-04-05T10:03:00+00:00")

            first = manager.mark_sent(session, now=datetime(2026, 4, 5, 12, 3, tzinfo=ZoneInfo("Europe/Berlin")))
            self.assertEqual(first["sent_count"], 1)
            self.assertEqual(first["next_run_at"], "2026-04-05T10:10:00+00:00")

            second = manager.mark_sent(first, now=datetime(2026, 4, 5, 12, 10, tzinfo=ZoneInfo("Europe/Berlin")))
            self.assertEqual(second["sent_count"], 2)
            self.assertEqual(second["next_run_at"], "2026-04-05T10:15:00+00:00")

            third = manager.mark_sent(second, now=datetime(2026, 4, 5, 12, 15, tzinfo=ZoneInfo("Europe/Berlin")))
            self.assertEqual(third["sent_count"], 3)
            self.assertEqual(third["next_run_at"], "2026-04-05T10:20:00+00:00")

    def test_chat_reset_runner_sends_two_button_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self.module.ChatResetSessionManager(db_path=str(Path(tmpdir) / "jarvis.db"))
            session = manager.start_session(now=self.now, force_new=True)
            notifier = FakeNotifier()
            runner = self.module.ChatResetDeliveryRunner(
                session_manager=manager,
                notifier=notifier,
                poll_interval_seconds=1,
            )

            delivered = runner.run_once(now=datetime(2026, 4, 5, 12, 4, tzinfo=ZoneInfo("Europe/Berlin")))

            self.assertEqual(delivered, 1)
            self.assertEqual(len(notifier.messages), 1)
            self.assertIn("reset", notifier.messages[0][0].lower())
            reply_markup = notifier.messages[0][1]
            self.assertIsNotNone(reply_markup)
            assert reply_markup is not None
            self.assertEqual(len(reply_markup.inline_keyboard), 1)
            self.assertEqual(len(reply_markup.inline_keyboard[0]), 2)
            updated = manager.get_session(session["id"])
            self.assertIsNotNone(updated)
            self.assertEqual(updated["sent_count"], 1)


if __name__ == "__main__":
    unittest.main()
