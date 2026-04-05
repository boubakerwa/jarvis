import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def create_memory_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            summary TEXT NOT NULL,
            category TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence TEXT NOT NULL,
            document_ref TEXT,
            supersedes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS financial_records (
            id TEXT PRIMARY KEY,
            drive_file_id TEXT,
            vendor TEXT,
            amount REAL,
            currency TEXT DEFAULT 'EUR',
            category TEXT,
            date TEXT,
            description TEXT,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO memories (
            id, topic, summary, category, source, confidence, document_ref,
            supersedes, created_at, updated_at, active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "mem-1",
            "travel passport",
            "User prefers aisle seats on flights.",
            "preference",
            "telegram",
            "high",
            "doc-123",
            None,
            "2026-04-05T14:00:00+00:00",
            "2026-04-05T15:00:00+00:00",
            1,
        ),
    )
    conn.execute(
        """
        INSERT INTO memories (
            id, topic, summary, category, source, confidence, document_ref,
            supersedes, created_at, updated_at, active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "mem-2",
            "old memory",
            "Inactive row should not show.",
            "fact",
            "manual",
            "low",
            None,
            None,
            "2026-04-01T14:00:00+00:00",
            "2026-04-01T15:00:00+00:00",
            0,
        ),
    )
    conn.commit()
    conn.close()


class DashboardTests(unittest.TestCase):
    def test_default_snapshot_skips_drive_loading(self):
        with TemporaryDirectory() as td:
            temp_root = Path(td)
            (temp_root / "logs").mkdir()
            (temp_root / "data").mkdir()
            (temp_root / "logs" / "jarvis.log").write_text(
                "\n".join(
                    [
                        "2026-04-05 15:00:00 [INFO] __main__: Starting Jarvis...",
                        "2026-04-05 15:03:00 [INFO] telegram.ext.Application: Application started",
                    ]
                )
            )
            create_memory_db(temp_root / "data" / "jarvis_memory.db")
            module = load_module("tested_dashboard_no_drive", "dashboard/app.py")
            module.ROOT = temp_root
            module.LOG_PATH = temp_root / "logs" / "jarvis.log"
            module.DB_PATH = temp_root / "data" / "jarvis_memory.db"
            module.GMAIL_ACTIVITY_PATH = temp_root / "data" / "gmail_activity.jsonl"
            module.GMAIL_STATE_PATH = temp_root / "data" / "gmail_state.txt"
            module.TOKEN_PATH = temp_root / "token.json"

            def fail_drive_load():
                raise AssertionError("drive snapshot should not be loaded")

            module._load_drive_snapshot = fail_drive_load
            snapshot = module.collect_snapshot()

        self.assertEqual(snapshot.drive_status, "idle")
        self.assertEqual(snapshot.drive_files, [])

    def test_dashboard_header_inlines_logo_svg(self):
        with TemporaryDirectory() as td:
            temp_root = Path(td)
            (temp_root / "logs").mkdir()
            (temp_root / "data").mkdir()
            (temp_root / "logs" / "jarvis.log").write_text(
                "\n".join(
                    [
                        "2026-04-05 15:00:00 [INFO] __main__: Starting Jarvis...",
                        "2026-04-05 15:03:00 [INFO] telegram.ext.Application: Application started",
                    ]
                )
            )
            create_memory_db(temp_root / "data" / "jarvis_memory.db")
            module = load_module("tested_dashboard_logo", "dashboard/app.py")
            module.ROOT = temp_root
            module.LOG_PATH = temp_root / "logs" / "jarvis.log"
            module.DB_PATH = temp_root / "data" / "jarvis_memory.db"
            module.GMAIL_ACTIVITY_PATH = temp_root / "data" / "gmail_activity.jsonl"
            module.GMAIL_STATE_PATH = temp_root / "data" / "gmail_state.txt"
            module.TOKEN_PATH = temp_root / "token.json"
            module.LOGO_PATH = temp_root / "dashboard" / "assets" / "jarvis-mark.svg"
            module.LOGO_PATH.parent.mkdir(parents=True)
            module.LOGO_PATH.write_text(
                """<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 64 64\">
  <path d=\"M39 18v21c0 6-4 10-10 10s-10-4-10-10\" />
</svg>
""",
                encoding="utf-8",
            )
            module._load_drive_snapshot = lambda: ([], "unavailable", "credentials missing")

            snapshot = module.collect_snapshot()
            html = module._render_snapshot(snapshot, tab="overview")

        self.assertIn("<svg", html)
        self.assertIn("Dashboard", html)
        self.assertIn("Updated", html)
        self.assertNotIn('http-equiv="refresh"', html)
        self.assertIn('id="tab-content"', html)

    def test_memory_tab_renders_active_memories(self):
        with TemporaryDirectory() as td:
            temp_root = Path(td)
            (temp_root / "logs").mkdir()
            (temp_root / "data").mkdir()
            (temp_root / "logs" / "jarvis.log").write_text(
                "\n".join(
                    [
                        "2026-04-05 15:00:00 [INFO] __main__: Starting Jarvis...",
                        "2026-04-05 15:03:00 [INFO] telegram.ext.Application: Application started",
                    ]
                )
            )
            create_memory_db(temp_root / "data" / "jarvis_memory.db")
            (temp_root / "data" / "gmail_activity.jsonl").write_text(
                json.dumps(
                    {
                        "processed_at": "2026-04-05T15:02:00+00:00",
                        "message_id": "1",
                        "thread_id": "t1",
                        "from": "A <a@example.com>",
                        "subject": "A",
                        "date": "Sat, 05 Apr 2026 15:02:00 +0000",
                        "attachment_count": 0,
                        "outcome": "skipped",
                        "reason": "not worth filing",
                    }
                )
                + "\n"
            )
            module = load_module("tested_dashboard_memory", "dashboard/app.py")
            module.ROOT = temp_root
            module.LOG_PATH = temp_root / "logs" / "jarvis.log"
            module.DB_PATH = temp_root / "data" / "jarvis_memory.db"
            module.GMAIL_ACTIVITY_PATH = temp_root / "data" / "gmail_activity.jsonl"
            module.GMAIL_STATE_PATH = temp_root / "data" / "gmail_state.txt"
            module.TOKEN_PATH = temp_root / "token.json"
            module._load_drive_snapshot = lambda: ([], "unavailable", "credentials missing")

            snapshot = module.collect_snapshot(include_memories=True)
            html = module._render_snapshot(snapshot, tab="memory")

        self.assertEqual(snapshot.app_status, "running")
        self.assertEqual(snapshot.memory_count, "1")
        self.assertEqual(len(snapshot.active_memories), 1)
        self.assertIn("travel passport", html)
        self.assertIn("https://drive.google.com/file/d/doc-123/view", html)
        self.assertIn('data-tab="memory"', html)

    def test_drive_tab_renders_clickable_links(self):
        with TemporaryDirectory() as td:
            temp_root = Path(td)
            (temp_root / "logs").mkdir()
            (temp_root / "data").mkdir()
            (temp_root / "logs" / "jarvis.log").write_text(
                "\n".join(
                    [
                        "2026-04-05 15:00:00 [INFO] __main__: Starting Jarvis...",
                        "2026-04-05 15:03:00 [INFO] telegram.ext.Application: Application started",
                    ]
                )
            )
            create_memory_db(temp_root / "data" / "jarvis_memory.db")
            module = load_module("tested_dashboard_drive", "dashboard/app.py")
            module.ROOT = temp_root
            module.LOG_PATH = temp_root / "logs" / "jarvis.log"
            module.DB_PATH = temp_root / "data" / "jarvis_memory.db"
            module.GMAIL_ACTIVITY_PATH = temp_root / "data" / "gmail_activity.jsonl"
            module.GMAIL_STATE_PATH = temp_root / "data" / "gmail_state.txt"
            module.TOKEN_PATH = temp_root / "token.json"
            module._load_drive_snapshot = lambda: (
                [
                    module.DriveFileItem(
                        path="Jarvis/Travel/Bookings/passport.pdf",
                        name="passport.pdf",
                        mime_type="application/pdf",
                        modified_time="2026-04-05T15:20:00+00:00",
                        web_view_link="https://drive.google.com/file/d/drive-123/view",
                    )
                ],
                "connected",
                "live drive data",
            )

            snapshot = module.collect_snapshot(include_drive=True)
            html = module._render_snapshot(snapshot, tab="drive")

        self.assertEqual(snapshot.drive_status, "connected")
        self.assertEqual(len(snapshot.drive_files), 1)
        self.assertIn("Jarvis/Travel/Bookings/passport.pdf", html)
        self.assertIn("https://drive.google.com/file/d/drive-123/view", html)
        self.assertIn('data-tab="drive"', html)

    def test_drive_tab_degrades_gracefully_when_unavailable(self):
        with TemporaryDirectory() as td:
            temp_root = Path(td)
            (temp_root / "logs").mkdir()
            (temp_root / "data").mkdir()
            (temp_root / "logs" / "jarvis.log").write_text(
                "\n".join(
                    [
                        "2026-04-05 15:00:00 [INFO] __main__: Starting Jarvis...",
                        "2026-04-05 15:03:00 [INFO] telegram.ext.Application: Application started",
                    ]
                )
            )
            create_memory_db(temp_root / "data" / "jarvis_memory.db")
            module = load_module("tested_dashboard_drive_missing", "dashboard/app.py")
            module.ROOT = temp_root
            module.LOG_PATH = temp_root / "logs" / "jarvis.log"
            module.DB_PATH = temp_root / "data" / "jarvis_memory.db"
            module.GMAIL_ACTIVITY_PATH = temp_root / "data" / "gmail_activity.jsonl"
            module.GMAIL_STATE_PATH = temp_root / "data" / "gmail_state.txt"
            module.TOKEN_PATH = temp_root / "token.json"
            module._load_drive_snapshot = lambda: ([], "unavailable", "Google credentials/token not available")

            snapshot = module.collect_snapshot(include_drive=True)
            html = module._render_snapshot(snapshot, tab="drive")

        self.assertEqual(snapshot.drive_status, "unavailable")
        self.assertIn("Google credentials/token not available", html)
        self.assertIn("No Drive files found under the managed Drive root.", html)

    def test_dashboard_polling_script_is_abort_safe(self):
        with TemporaryDirectory() as td:
            temp_root = Path(td)
            (temp_root / "logs").mkdir()
            (temp_root / "data").mkdir()
            (temp_root / "logs" / "jarvis.log").write_text(
                "\n".join(
                    [
                        "2026-04-05 15:00:00 [INFO] __main__: Starting Jarvis...",
                        "2026-04-05 15:03:00 [INFO] telegram.ext.Application: Application started",
                    ]
                )
            )
            create_memory_db(temp_root / "data" / "jarvis_memory.db")
            module = load_module("tested_dashboard_polling", "dashboard/app.py")
            module.ROOT = temp_root
            module.LOG_PATH = temp_root / "logs" / "jarvis.log"
            module.DB_PATH = temp_root / "data" / "jarvis_memory.db"
            module.GMAIL_ACTIVITY_PATH = temp_root / "data" / "gmail_activity.jsonl"
            module.GMAIL_STATE_PATH = temp_root / "data" / "gmail_state.txt"
            module.TOKEN_PATH = temp_root / "token.json"
            module._load_drive_snapshot = lambda: ([], "unavailable", "credentials missing")

            snapshot = module.collect_snapshot()
            html = module._render_snapshot(snapshot, tab="drive")

        self.assertIn("new URL(path, window.location.href)", html)
        self.assertIn('credentials: "same-origin"', html)
        self.assertIn("pagehide", html)
        self.assertIn("beforeunload", html)
        self.assertIn("visibilitychange", html)
        self.assertIn("AbortController", html)
        self.assertIn('document.visibilityState !== "visible"', html)


if __name__ == "__main__":
    unittest.main()
