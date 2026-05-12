import tempfile
import unittest

from gmail.action_extractor import (
    EmailActionProposal,
    ProposedCalendarEvent,
    ProposedMemoryUpdate,
    ProposedReminder,
    ProposedTask,
)
from gmail.action_store import GmailActionManager, format_gmail_action_card


class FakeMemory:
    def __init__(self):
        self.tasks = []
        self.memories = []

    def create_task(self, description, due_date=None, *, source="manual", surfaced=True):
        task = {"id": "task-1", "description": description, "due_date": due_date, "source": source, "surfaced": surfaced}
        self.tasks.append(task)
        return task

    def upsert(self, record):
        self.memories.append(record)
        return record


class FakeCalendar:
    def __init__(self):
        self.events = []

    def create_event(self, **kwargs):
        self.events.append(kwargs)
        return {"summary": kwargs["summary"]}


class FakeReminders:
    def __init__(self):
        self.reminders = []

    def schedule_message(self, message, when):
        reminder = {"id": "reminder-1", "message": message, "when": when}
        self.reminders.append(reminder)
        return reminder


class GmailActionManagerTests(unittest.TestCase):
    def test_store_and_commit_proposal_applies_side_effects_after_confirm(self):
        with tempfile.NamedTemporaryFile() as db:
            memory = FakeMemory()
            calendar = FakeCalendar()
            reminders = FakeReminders()
            manager = GmailActionManager(
                memory_manager=memory,
                calendar_client=calendar,
                reminder_manager=reminders,
                db_path=db.name,
            )
            proposal = EmailActionProposal(
                message_id="msg-1",
                thread_id="thread-1",
                sender="sender@example.com",
                subject="Planning",
                calendar_events=(ProposedCalendarEvent(summary="Review", start="2026-05-13T10:00:00+02:00"),),
                tasks=(ProposedTask(description="Send agenda", due_date="2026-05-13"),),
                reminders=(ProposedReminder(message="Follow up", when="2026-05-13T09:00:00+02:00"),),
                memory_updates=(ProposedMemoryUpdate(topic="client:acme", summary="Prefers morning calls"),),
                reply_bullets=("Confirm agenda",),
            )

            stored = manager.store_proposal(proposal)

            self.assertEqual(memory.tasks, [])
            self.assertEqual(calendar.events, [])
            committed, results = manager.commit_proposal(stored["id"])

            self.assertEqual(committed["status"], "committed")
            self.assertEqual(len(memory.tasks), 1)
            self.assertEqual(len(calendar.events), 1)
            self.assertEqual(len(reminders.reminders), 1)
            self.assertEqual(len(memory.memories), 1)
            self.assertTrue(any("Created task" in result for result in results))

    def test_format_card_includes_confirm_semantics(self):
        proposal = {
            "id": "proposal-1",
            "payload": {
                "sender": "sender@example.com",
                "subject": "Planning",
                "summary": "Meeting needs an agenda.",
                "tasks": [{"description": "Send agenda"}],
            },
        }

        card = format_gmail_action_card(proposal)

        self.assertIn("[Gmail] Proposed actions", card)
        self.assertIn("Send agenda", card)
        self.assertIn("Confirm to create", card)


if __name__ == "__main__":
    unittest.main()
