import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from config import settings


OLD_LINKEDIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS linkedin_drafts (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending_generation',
    voice TEXT NOT NULL DEFAULT 'professional',
    origin TEXT NOT NULL DEFAULT 'telegram',
    source_text TEXT NOT NULL,
    source_author TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'manual',
    rewrite_of TEXT NOT NULL DEFAULT '',
    rewrite_instructions TEXT NOT NULL DEFAULT '',
    preset_id TEXT NOT NULL DEFAULT '',
    pillar_id TEXT NOT NULL DEFAULT '',
    pillar_label TEXT NOT NULL DEFAULT '',
    library_tags TEXT NOT NULL DEFAULT '[]',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_attempt_at TEXT NOT NULL DEFAULT '',
    obsidian_path TEXT NOT NULL DEFAULT '',
    obsidian_filename TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def insert_old_draft(db_path: Path, draft_id: str = "draft-1") -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(OLD_LINKEDIN_SCHEMA)
    conn.execute(
        """
        INSERT INTO linkedin_drafts (
            id, status, voice, origin, source_text, source_author, source_url,
            source_type, rewrite_of, rewrite_instructions, preset_id, pillar_id,
            pillar_label, library_tags, attempts, last_error, last_attempt_at,
            obsidian_path, obsidian_filename, created_at, updated_at
        ) VALUES (?, 'ready', 'operator', 'telegram', ?, 'Source', ?, 'x-post',
                  '', '', '', '', 'Operator Commentary', '["x-sourced"]', 0, '', '',
                  'LinkedIn/2026-05/post.md', 'post', ?, ?)
        """,
        (
            draft_id,
            "New AI paper changes enterprise adoption.",
            "https://x.com/source/status/1",
            "2026-05-12T08:00:00+00:00",
            "2026-05-12T08:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()


class LinkedInEditorialTests(unittest.TestCase):
    def setUp(self):
        self.original_db = settings.JARVIS_DB_PATH
        self.original_tz = settings.JARVIS_TIMEZONE
        settings.JARVIS_TIMEZONE = "Europe/Berlin"
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jarvis.db"
        settings.JARVIS_DB_PATH = str(self.db_path)

    def tearDown(self):
        settings.JARVIS_DB_PATH = self.original_db
        settings.JARVIS_TIMEZONE = self.original_tz
        self.tmp.cleanup()

    def test_old_linkedin_table_migrates_and_tracks_publish_flow(self):
        insert_old_draft(self.db_path)
        from linkedin import sqlite_store

        row = sqlite_store.get_by_id("draft-1")
        self.assertEqual(row["publish_status"], "unscheduled")
        self.assertEqual(row["link_policy"], "first_comment")

        scheduled = sqlite_store.schedule_draft("draft-1", "2026-05-12T07:00:00+00:00")
        self.assertEqual(scheduled["publish_status"], "scheduled")

        due = sqlite_store.list_due_publish_reminders(
            now=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(len(due), 1)

        sqlite_store.mark_publish_reminded("draft-1", reminded_at="2026-05-12T08:00:00+00:00")
        due_again = sqlite_store.list_due_publish_reminders(
            now=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(due_again, [])

        published = sqlite_store.mark_published("draft-1", linkedin_url="https://linkedin.com/posts/1")
        self.assertEqual(published["publish_status"], "published")
        self.assertEqual(published["linkedin_url"], "https://linkedin.com/posts/1")

    def test_next_publish_slots_use_tuesday_thursday_morning(self):
        from linkedin.editorial import next_publish_slots

        slots = next_publish_slots(
            now=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
            count=2,
        )

        self.assertEqual(slots[0]["local_label"], "Thu 14 May 09:00")
        self.assertEqual(slots[1]["local_label"], "Tue 19 May 09:00")

    def test_publish_reminder_formats_and_marks_sent(self):
        insert_old_draft(self.db_path)
        from linkedin import sqlite_store
        from linkedin.editorial import process_publish_reminders

        sqlite_store.schedule_draft("draft-1", "2026-05-12T07:00:00+00:00")

        class FakeNotifier:
            def __init__(self):
                self.messages = []

            def send_message(self, message):
                self.messages.append(message)
                return True

        notifier = FakeNotifier()
        summary = process_publish_reminders(
            notifier,
            now=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(summary["sent"], 1)
        self.assertIn("[LinkedIn] Scheduled post is due", notifier.messages[0])
        self.assertEqual(sqlite_store.list_due_publish_reminders(now=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)), [])


if __name__ == "__main__":
    unittest.main()
