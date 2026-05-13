import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_planner import service


class FakeMemory:
    def __init__(self):
        self.created = []

    def create_task(self, description, due_date=None, *, source="manual", surfaced=True):
        task = {
            "id": f"task-{len(self.created) + 1}",
            "description": description,
            "due_date": due_date,
            "source": source,
            "surfaced": surfaced,
        }
        self.created.append(task)
        return task

    def list_tasks(self, status):
        return []


class FakeReminders:
    def __init__(self):
        self.scheduled = []

    def schedule_message(self, message, when, *, recurrence=None, task_id=None, until_task_done=False, now=None):
        reminder = {
            "id": f"reminder-{len(self.scheduled) + 1}",
            "message": message,
            "when": when,
            "task_id": task_id,
            "until_task_done": until_task_done,
        }
        self.scheduled.append(reminder)
        return reminder

    def list_reminders(self, status):
        return []


def _task(index, title, urgency="medium", estimate=60, dependency="", window_start="", window_end=""):
    return service.PlannerTask(
        title=title,
        urgency=urgency,
        estimate_minutes=estimate,
        dependency=dependency,
        window_start=window_start,
        window_end=window_end,
        original_index=index,
    )


class DailyPlannerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 12, 8, 30, tzinfo=ZoneInfo("Europe/Berlin"))

    def test_next_run_skips_sunday(self):
        sunday = datetime(2026, 4, 5, 8, 0, tzinfo=ZoneInfo("Europe/Berlin"))

        seconds = service.seconds_until_next_planner_run(sunday)

        self.assertEqual(seconds, 24.5 * 60 * 60)

    def test_build_plan_respects_dependency_windows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = service.DailyPlannerManager(db_path=str(Path(tmpdir) / "jarvis.db"))
            tasks = [
                _task(1, "Call tax office", "high", 30, window_start="10:00"),
                _task(2, "Prepare notes", "medium", 60),
                _task(3, "Visit counter", "high", 90, window_start="09:00", window_end="10:00"),
            ]

            plan = manager.build_plan(tasks, now=self.now)

            self.assertEqual(plan.scheduled[0].task.title, "Prepare notes")
            self.assertEqual(plan.scheduled[0].start.strftime("%H:%M"), "08:30")
            self.assertEqual(plan.scheduled[1].task.title, "Call tax office")
            self.assertEqual(plan.scheduled[1].start.strftime("%H:%M"), "10:00")
            self.assertEqual(plan.unscheduled[0]["task"]["title"], "Visit counter")

    def test_fit_plan_creates_tasks_and_linked_reminders(self):
        memory = FakeMemory()
        reminders = FakeReminders()
        parser = lambda _text: [
            _task(1, "Write proposal", "high", 60),
            _task(2, "Answer invoices", "medium", 30),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = service.DailyPlannerManager(
                memory_manager=memory,
                reminder_manager=reminders,
                db_path=str(Path(tmpdir) / "jarvis.db"),
                parser=parser,
            )
            manager.start_today_session(now=self.now)

            result = manager.handle_user_message("tasks", now=self.now)

            self.assertTrue(result.handled)
            self.assertIn("Today's realistic plan", result.text)
            self.assertEqual([task["description"] for task in memory.created], ["Write proposal", "Answer invoices"])
            self.assertEqual(len(reminders.scheduled), 2)
            self.assertEqual(reminders.scheduled[0]["task_id"], "task-1")
            self.assertTrue(reminders.scheduled[0]["until_task_done"])

    def test_overflow_waits_for_prioritization_before_scheduling(self):
        memory = FakeMemory()
        reminders = FakeReminders()
        parser = lambda _text: [_task(index, f"Task {index}", "high", 180) for index in range(1, 6)]
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = service.DailyPlannerManager(
                memory_manager=memory,
                reminder_manager=reminders,
                db_path=str(Path(tmpdir) / "jarvis.db"),
                parser=parser,
            )
            session = manager.start_today_session(now=self.now)

            result = manager.handle_user_message("too much", now=self.now)

            self.assertIn("No reminders have been scheduled yet", result.text)
            self.assertIn("1. Task 1", result.text)
            self.assertEqual(memory.created, [])
            self.assertEqual(reminders.scheduled, [])
            stored = manager._db.execute("SELECT status FROM daily_planner_sessions WHERE id=?", (session["id"],)).fetchone()
            self.assertEqual(stored["status"], "awaiting_prioritization")

            prioritized = manager.handle_user_message("1, 2", now=self.now)

            self.assertIn("Today's realistic plan", prioritized.text)
            self.assertEqual(len(memory.created), 2)
            self.assertEqual(len(reminders.scheduled), 2)

    def test_validation_requires_urgency_and_estimate_or_complexity(self):
        with self.assertRaises(ValueError) as raised:
            service._validate_parsed_tasks({"tasks": [{"title": "Vague task"}]})

        message = str(raised.exception)
        self.assertIn("missing urgency", message)
        self.assertIn("missing estimate or complexity", message)

    def test_complexity_maps_to_default_estimate(self):
        tasks = service._validate_parsed_tasks(
            {"tasks": [{"title": "Hard thing", "urgency": "high", "complexity": "complex"}]}
        )

        self.assertEqual(tasks[0].estimate_minutes, 120)


if __name__ == "__main__":
    unittest.main()
