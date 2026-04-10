import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
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

    def send_message(self, text: str) -> bool:
        self.messages.append(text)
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
            self.assertEqual(notifier.messages, ["Ping Wess"])
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


if __name__ == "__main__":
    unittest.main()
