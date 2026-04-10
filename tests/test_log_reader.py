import importlib.util
import json
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


def write_jsonl(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(record) + "\n" for record in records)
    path.write_text(payload, encoding="utf-8")


class LogReaderTests(unittest.TestCase):
    def test_read_logs_filters_by_date_and_level(self):
        module = load_module("tested_log_reader", "core/log_reader.py")

        with TemporaryDirectory() as td:
            root = Path(td)
            activity_path = root / "ops_activity.jsonl"
            issues_path = root / "ops_issues.jsonl"
            audit_path = root / "ops_audit.jsonl"
            write_jsonl(
                activity_path,
                {
                    "ts": "2026-04-10T11:00:00+00:00",
                    "kind": "activity",
                    "level": "INFO",
                    "component": "runtime",
                    "event": "heartbeat",
                    "status": "ok",
                    "summary": "Heartbeat",
                },
            )
            write_jsonl(
                issues_path,
                {
                    "ts": "2026-04-10T12:00:00+00:00",
                    "kind": "issue",
                    "level": "ERROR",
                    "component": "reminders",
                    "event": "reminder_send_failed",
                    "status": "warning",
                    "summary": "Reminder failed",
                    "metadata": {"reminder_id": "abc"},
                },
                {
                    "ts": "2026-04-09T12:00:00+00:00",
                    "kind": "issue",
                    "level": "ERROR",
                    "component": "gmail",
                    "event": "old_error",
                    "status": "error",
                    "summary": "Old error",
                },
            )
            write_jsonl(
                audit_path,
                {
                    "ts": "2026-04-10T13:00:00+00:00",
                    "kind": "audit",
                    "level": "INFO",
                    "component": "memory",
                    "event": "memory_ready",
                    "status": "ok",
                    "summary": "Memory ready",
                },
            )
            module._LOG_SOURCES = (
                ("ops_activity", activity_path),
                ("ops_issues", issues_path),
                ("ops_audit", audit_path),
            )

            records = module.read_logs(date_expression="2026-04-10", level="ERROR", limit=10)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"], "ops_issues")
        self.assertEqual(records[0]["event"], "reminder_send_failed")
        self.assertEqual(records[0]["level"], "ERROR")

    def test_read_logs_orders_newest_first_and_applies_limit(self):
        module = load_module("tested_log_reader_limit", "core/log_reader.py")

        with TemporaryDirectory() as td:
            root = Path(td)
            path = root / "ops_activity.jsonl"
            write_jsonl(
                path,
                {
                    "ts": "2026-04-10T10:00:00+00:00",
                    "kind": "activity",
                    "level": "INFO",
                    "component": "runtime",
                    "event": "first",
                    "status": "ok",
                    "summary": "First",
                },
                {
                    "ts": "2026-04-10T11:00:00+00:00",
                    "kind": "activity",
                    "level": "INFO",
                    "component": "runtime",
                    "event": "second",
                    "status": "ok",
                    "summary": "Second",
                },
            )
            module._LOG_SOURCES = (("ops_activity", path),)

            records = module.read_logs(limit=1)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "second")


if __name__ == "__main__":
    unittest.main()
