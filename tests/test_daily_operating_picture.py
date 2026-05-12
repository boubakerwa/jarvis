import unittest
from datetime import datetime, timezone

from github_issues.models import IssueSummary
from morning_digest import digest


class FakeMemory:
    def list_tasks(self, status):
        return [{"description": "Prepare investor update", "due_date": "2026-05-12"}]


class FakeReminders:
    def list_reminders(self, status):
        return [{"message": "Pay invoice", "next_run_at": "2026-05-12T10:30:00+00:00"}]


class FakeCalendar:
    def get_events(self, time_min, time_max, max_results=20):
        return [{"summary": "Design review", "start": "2026-05-12T09:30:00+00:00", "location": "Meet"}]


class DailyOperatingPictureTests(unittest.TestCase):
    def test_message_combines_cross_system_sections_and_top_three(self):
        original_fetch_issues = digest._fetch_open_issues
        original_fetch_linkedin = digest._fetch_linkedin_drafts
        original_read_gmail = digest._read_recent_gmail_activity
        try:
            digest._fetch_open_issues = lambda limit=10: [
                IssueSummary(
                    number=3,
                    title="Fix Gmail action cards",
                    state="open",
                    url="https://github.com/owner/repo/issues/3",
                    labels=("bug",),
                )
            ]
            digest._fetch_linkedin_drafts = lambda limit=6: [
                {"id": "abc12345", "status": "ready", "source_text": "Launch post"}
            ]
            digest._read_recent_gmail_activity = lambda limit=8: [
                {"outcome": "filed", "subject": "Bank statement", "from": "bank@example.com"}
            ]

            message = digest.build_daily_operating_picture(
                now=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
                memory_manager=FakeMemory(),
                reminder_manager=FakeReminders(),
                calendar_client=FakeCalendar(),
            )
        finally:
            digest._fetch_open_issues = original_fetch_issues
            digest._fetch_linkedin_drafts = original_fetch_linkedin
            digest._read_recent_gmail_activity = original_read_gmail

        self.assertIn("If you only do three things", message)
        self.assertIn("Calendar, next", message)
        self.assertIn("Pending tasks", message)
        self.assertIn("Active reminders", message)
        self.assertIn("Recent Gmail outcomes", message)
        self.assertIn("LinkedIn draft backlog", message)
        self.assertIn("Open GitHub issues", message)
        self.assertIn("Fix Gmail action cards", message)


if __name__ == "__main__":
    unittest.main()
