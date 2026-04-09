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


def insert_linkedin_draft(
    path: Path,
    *,
    draft_id: str = "draft-12345678",
    obsidian_path: str = "",
    obsidian_filename: str = "",
    status: str = "ready",
    source_text: str = "Source text",
) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
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
    )
    conn.execute(
        """
        INSERT INTO linkedin_drafts (
            id, status, voice, origin, source_text, source_author, source_url,
            source_type, rewrite_of, rewrite_instructions, preset_id, pillar_id,
            pillar_label, library_tags, attempts, last_error, last_attempt_at,
            obsidian_path, obsidian_filename, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            draft_id,
            status,
            "professional",
            "telegram",
            source_text,
            "Source Author",
            "https://example.com/post",
            "manual",
            "",
            "",
            "",
            "",
            "Operator Commentary",
            '["x-sourced"]',
            0,
            "",
            "",
            obsidian_path,
            obsidian_filename,
            "2026-04-09T09:00:00+00:00",
            "2026-04-09T09:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()


def write_llm_activity(path: Path, *records: dict) -> None:
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + ("\n" if records else ""),
        encoding="utf-8",
    )


def write_jsonl(path: Path, *records: dict) -> None:
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + ("\n" if records else ""),
        encoding="utf-8",
    )


def configure_dashboard_module(module, temp_root: Path) -> None:
    module.ROOT = temp_root
    module.LOG_PATH = temp_root / "logs" / "jarvis.log"
    module.DB_PATH = temp_root / "data" / "jarvis_memory.db"
    module.GMAIL_ACTIVITY_PATH = temp_root / "data" / "gmail_activity.jsonl"
    module.GMAIL_STATE_PATH = temp_root / "data" / "gmail_state.txt"
    module.TOKEN_PATH = temp_root / "token.json"
    module.LLM_ACTIVITY_PATH = temp_root / "data" / "llm_activity.jsonl"
    module.OPS_ACTIVITY_PATH = temp_root / "data" / "ops_activity.jsonl"
    module.OPS_ISSUES_PATH = temp_root / "data" / "ops_issues.jsonl"
    module.OPS_AUDIT_PATH = temp_root / "data" / "ops_audit.jsonl"
    module.settings.JARVIS_DB_PATH = str(temp_root / "data" / "jarvis_memory.db")
    module.settings.OBSIDIAN_VAULT_PATH = str(temp_root / "vault")
    module.settings.OBSIDIAN_ROOT_FOLDER = "."


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
            configure_dashboard_module(module, temp_root)

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
            configure_dashboard_module(module, temp_root)
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
            configure_dashboard_module(module, temp_root)
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
            configure_dashboard_module(module, temp_root)
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
            configure_dashboard_module(module, temp_root)
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
            configure_dashboard_module(module, temp_root)
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

    def test_llmops_tab_renders_usage_and_costs(self):
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
            write_llm_activity(
                temp_root / "data" / "llm_activity.jsonl",
                {
                    "recorded_at": "2026-04-05T15:05:00+00:00",
                    "task": "chat",
                    "model": "anthropic/claude-sonnet-4.6",
                    "status": "ok",
                    "latency_ms": 820.5,
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "total_tokens": 1200,
                    "estimated_cost_usd": 0.006,
                    "error": "",
                },
                {
                    "recorded_at": "2026-04-05T15:06:00+00:00",
                    "task": "relevance",
                    "model": "google/gemma-4-31b-it",
                    "status": "validation_error",
                    "latency_ms": 120.0,
                    "input_tokens": 80,
                    "output_tokens": 30,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "total_tokens": 110,
                    "estimated_cost_usd": None,
                    "error": "No JSON object found in model response.",
                },
            )
            write_jsonl(
                temp_root / "data" / "ops_activity.jsonl",
                {
                    "ts": "2026-04-05T15:07:00+00:00",
                    "kind": "activity",
                    "level": "INFO",
                    "component": "runtime",
                    "event": "app_heartbeat",
                    "status": "ok",
                    "summary": "Marvis heartbeat",
                },
            )
            write_jsonl(
                temp_root / "data" / "ops_issues.jsonl",
                {
                    "ts": "2026-04-05T15:04:00+00:00",
                    "kind": "issue",
                    "level": "WARNING",
                    "component": "gmail",
                    "event": "email_processing_partial",
                    "status": "partial",
                    "summary": "Email processing completed with partial failures",
                    "duration_ms": 531.2,
                },
                {
                    "ts": "2026-04-05T15:06:30+00:00",
                    "kind": "issue",
                    "level": "ERROR",
                    "component": "telegram",
                    "event": "telegram_file_filing_failed",
                    "status": "error",
                    "summary": "Failed to file Telegram document",
                },
            )
            write_jsonl(
                temp_root / "data" / "ops_audit.jsonl",
                {
                    "ts": "2026-04-05T15:06:45+00:00",
                    "kind": "audit",
                    "level": "INFO",
                    "component": "memory",
                    "event": "task_created",
                    "status": "ok",
                    "summary": "Created task",
                },
            )
            module = load_module("tested_dashboard_llmops", "dashboard/app.py")
            configure_dashboard_module(module, temp_root)
            module._load_drive_snapshot = lambda: ([], "unavailable", "credentials missing")

            snapshot = module.collect_snapshot()
            html = module._render_snapshot(snapshot, tab="llmops")

        self.assertEqual(snapshot.llmops_summary.call_count, 2)
        self.assertEqual(snapshot.llmops_summary.total_tokens, 1310)
        self.assertAlmostEqual(snapshot.llmops_summary.estimated_cost_usd or 0.0, 0.006)
        self.assertIn("LLMOps", html)
        self.assertIn("Task Breakdown", html)
        self.assertIn("Recent Calls", html)
        self.assertIn("Operational Logging", html)
        self.assertIn("Charts", html)
        self.assertIn("LLM Cost by Hour", html)
        self.assertIn("Tokens by Task", html)
        self.assertIn("Issues by Component", html)
        self.assertIn("Heartbeat Timeline", html)
        self.assertIn("Issue Breakdown", html)
        self.assertIn("Recent Audit Events", html)
        self.assertIn("<svg", html)
        self.assertIn("anthropic/claude-sonnet-4.6", html)
        self.assertIn("validation_error", html)
        self.assertIn("$0.006000", html)
        self.assertIn("No JSON object found in model response.", html)
        self.assertIn("Email processing completed with partial failures", html)
        self.assertIn("Created task", html)

    def test_linkedin_tab_renders_editor_shell_and_open_buttons(self):
        with TemporaryDirectory() as td:
            temp_root = Path(td)
            (temp_root / "logs").mkdir()
            (temp_root / "data").mkdir()
            (temp_root / "vault" / "LinkedIn" / "2026-04").mkdir(parents=True)
            (temp_root / "logs" / "jarvis.log").write_text(
                "\n".join(
                    [
                        "2026-04-05 15:00:00 [INFO] __main__: Starting Jarvis...",
                        "2026-04-05 15:03:00 [INFO] telegram.ext.Application: Application started",
                    ]
                )
            )
            db_path = temp_root / "data" / "jarvis_memory.db"
            create_memory_db(db_path)
            note_path = "LinkedIn/2026-04/test-post_draft-12.md"
            (temp_root / "vault" / note_path).write_text(
                "---\ncreated_at: \"2026-04-09T09:00:00+00:00\"\n---\n\n# Test Post\n\nHello markdown world.\n",
                encoding="utf-8",
            )
            insert_linkedin_draft(
                db_path,
                draft_id="draft-12345678",
                obsidian_path=note_path,
                obsidian_filename="test_post_draft-12",
            )

            module = load_module("tested_dashboard_linkedin_shell", "dashboard/app.py")
            configure_dashboard_module(module, temp_root)
            snapshot = module.collect_snapshot(include_linkedin=True)
            html = module._render_snapshot(snapshot, tab="linkedin")

        self.assertIn('data-linkedin-root', html)
        self.assertIn('data-linkedin-open="draft-12345678"', html)
        self.assertIn("Open post", html)
        self.assertIn('data-linkedin-panel hidden', html)
        self.assertIn('replace(/\\r\\n/g, "\\n")', html)
        self.assertIn('source.split("\\n")', html)
        self.assertIn('trimmed.match(/^(#{1,6})\\s+(.*)$/)', html)
        self.assertIn('data-linkedin-save', html)

    def test_linkedin_editor_payload_and_save_round_trip_note(self):
        with TemporaryDirectory() as td:
            temp_root = Path(td)
            (temp_root / "logs").mkdir()
            (temp_root / "data").mkdir()
            (temp_root / "vault" / "LinkedIn" / "2026-04").mkdir(parents=True)
            (temp_root / "logs" / "jarvis.log").write_text(
                "\n".join(
                    [
                        "2026-04-05 15:00:00 [INFO] __main__: Starting Jarvis...",
                        "2026-04-05 15:03:00 [INFO] telegram.ext.Application: Application started",
                    ]
                )
            )
            db_path = temp_root / "data" / "jarvis_memory.db"
            create_memory_db(db_path)
            note_path = "LinkedIn/2026-04/test-post_draft-12.md"
            note_file = temp_root / "vault" / note_path
            note_file.write_text(
                "---\ncreated_at: \"2026-04-09T09:00:00+00:00\"\n---\n\n# Test Post\n\nHello markdown world.\n",
                encoding="utf-8",
            )
            insert_linkedin_draft(
                db_path,
                draft_id="draft-12345678",
                obsidian_path=note_path,
                obsidian_filename="test_post_draft-12",
            )

            module = load_module("tested_dashboard_linkedin_save", "dashboard/app.py")
            configure_dashboard_module(module, temp_root)

            payload, status_code = module._linkedin_editor_payload("draft-12")
            self.assertEqual(status_code, 200)
            self.assertTrue(payload["editable"])
            self.assertEqual(payload["draftId"], "draft-12345678")
            self.assertEqual(payload["headline"], "Test Post")
            self.assertIn("Hello markdown world.", payload["content"])

            updated_payload, updated_status = module._save_linkedin_draft_content(
                "draft-12",
                "# Test Post\n\nUpdated markdown body.\n",
            )
            saved_text = note_file.read_text(encoding="utf-8")

        self.assertEqual(updated_status, 200)
        self.assertEqual(updated_payload["detail"], "Saved to Obsidian.")
        self.assertIn("Updated markdown body.", saved_text)

    def test_linkedin_editor_payload_reports_fallback_when_note_read_fails(self):
        with TemporaryDirectory() as td:
            temp_root = Path(td)
            (temp_root / "logs").mkdir()
            (temp_root / "data").mkdir()
            (temp_root / "vault" / "LinkedIn" / "2026-04").mkdir(parents=True)
            (temp_root / "logs" / "jarvis.log").write_text(
                "\n".join(
                    [
                        "2026-04-05 15:00:00 [INFO] __main__: Starting Jarvis...",
                        "2026-04-05 15:03:00 [INFO] telegram.ext.Application: Application started",
                    ]
                )
            )
            db_path = temp_root / "data" / "jarvis_memory.db"
            create_memory_db(db_path)
            note_path = "LinkedIn/2026-04/test-post_draft-12.md"
            insert_linkedin_draft(
                db_path,
                draft_id="draft-12345678",
                obsidian_path=note_path,
                obsidian_filename="test_post_draft-12",
                source_text="Original source text",
            )

            module = load_module("tested_dashboard_linkedin_fallback", "dashboard/app.py")
            configure_dashboard_module(module, temp_root)

            original = module._read_note_content
            module._read_note_content = lambda *args, **kwargs: ("", "", False)
            try:
                payload, status_code = module._linkedin_editor_payload("draft-12")
            finally:
                module._read_note_content = original

        self.assertEqual(status_code, 200)
        self.assertEqual(
            payload["detail"],
            "Could not read the saved post from Obsidian. Showing a fallback scaffold.",
        )
        self.assertIn("Original source text", payload["content"])


if __name__ == "__main__":
    unittest.main()
