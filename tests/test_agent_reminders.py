import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeReminders:
    def __init__(self):
        self.scheduled = []
        self.cancelled = []

    def schedule_message(self, message, when, *, recurrence=None, task_id=None, until_task_done=False, now=None):
        reminder = {
            "id": "abcd1234-0000-0000-0000-000000000000",
            "message": message,
            "next_run_at": "2026-04-06T08:00:00+00:00",
            "status": "scheduled",
            "recurrence": recurrence,
            "task_id": task_id,
            "until_task_done": 1 if until_task_done else 0,
        }
        self.scheduled.append(
            {
                "message": message,
                "when": when,
                "recurrence": recurrence,
                "task_id": task_id,
                "until_task_done": until_task_done,
            }
        )
        return reminder

    def list_reminders(self, status):
        return [
            {
                "id": "abcd1234-0000-0000-0000-000000000000",
                "message": "Call doctor",
                "next_run_at": "2026-04-06T08:00:00+00:00",
                "status": status if status != "all" else "scheduled",
                "recurrence": None,
                "task_id": None,
            }
        ]

    def cancel_reminder(self, reminder_id, *, now=None):
        self.cancelled.append(reminder_id)
        if reminder_id == "missing":
            return None
        return {
            "id": "abcd1234-0000-0000-0000-000000000000",
            "message": "Call doctor",
            "next_run_at": "2026-04-06T08:00:00+00:00",
            "status": "cancelled",
            "recurrence": None,
            "task_id": None,
        }

    def describe_reminder(self, reminder):
        return f"[{reminder['id'][:8]}] {reminder['status']} for 2026-04-06 10:00 CEST (one-off) — {reminder['message']}"


class FakeMemory:
    def list_tasks(self, status="pending"):
        if status == "all":
            return [
                {
                    "id": "visible12-0000-0000-0000-000000000000",
                    "description": "Visible task",
                    "due_date": None,
                    "status": "pending",
                    "source": "manual",
                    "surfaced": 1,
                },
                {
                    "id": "remind123-0000-0000-0000-000000000000",
                    "description": "Reminder-backed task",
                    "due_date": "2026-04-06T08:00:00+00:00",
                    "status": "pending",
                    "source": "reminder",
                    "surfaced": 0,
                },
            ]
        return [
            {
                "id": "visible12-0000-0000-0000-000000000000",
                "description": "Visible task",
                "due_date": None,
                "status": "pending",
                "source": "manual",
                "surfaced": 1,
            }
        ]


class AgentReminderTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module("tested_agent_reminders", "core/agent.py")
        self.module.get_local_now = lambda: "ignored-now"

    def test_schedule_message_tool_returns_summary(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._reminders = FakeReminders()

        response = agent._tool_schedule_message(
            {
                "message": "Call doctor",
                "when": "tomorrow at 10am",
                "recurrence": "daily",
            }
        )

        self.assertIn("Reminder scheduled", response)
        self.assertIn("abcd1234", response)
        self.assertEqual(agent._reminders.scheduled[0]["recurrence"], "daily")

    def test_list_reminders_tool_formats_entries(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._reminders = FakeReminders()

        response = agent._tool_list_reminders({"status": "scheduled"})

        self.assertIn("Call doctor", response)
        self.assertIn("scheduled", response)

    def test_cancel_reminder_tool_handles_missing_reminder(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._reminders = FakeReminders()

        response = agent._tool_cancel_reminder({"reminder_id": "missing"})

        self.assertIn("No scheduled reminder found", response)

    def test_list_tasks_ignores_hidden_reminder_backing_tasks(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._memory = FakeMemory()

        response = agent._tool_list_tasks({"status": "pending"})

        self.assertIn("Visible task", response)

    def test_list_tasks_all_shows_hidden_reminder_backing_tasks_with_marker(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._memory = FakeMemory()

        response = agent._tool_list_tasks({"status": "all"})

        self.assertIn("Visible task", response)
        self.assertIn("Reminder-backed task", response)
        self.assertIn("[reminder]", response)


if __name__ == "__main__":
    unittest.main()
