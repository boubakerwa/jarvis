import importlib.util
import json
import logging
import sys
import unittest
from datetime import datetime, timedelta, timezone
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


class OpsLogTests(unittest.TestCase):
    def test_activity_and_issue_retention_prunes_old_records(self):
        module = load_module("tested_opslog_retention", "core/opslog.py")

        with TemporaryDirectory() as td:
            root = Path(td)
            module.OPS_ACTIVITY_PATH = root / "ops_activity.jsonl"
            module.OPS_ISSUES_PATH = root / "ops_issues.jsonl"
            module.OPS_AUDIT_PATH = root / "ops_audit.jsonl"
            module._LAST_PRUNE_AT.clear()

            old_activity_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0).isoformat()
            old_issue_ts = (datetime.now(timezone.utc) - timedelta(days=4)).replace(microsecond=0).isoformat()
            module.OPS_ACTIVITY_PATH.write_text(
                json.dumps(
                    {
                        "ts": old_activity_ts,
                        "kind": "activity",
                        "level": "INFO",
                        "component": "runtime",
                        "event": "old_activity",
                        "status": "ok",
                        "summary": "old",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            module.OPS_ISSUES_PATH.write_text(
                json.dumps(
                    {
                        "ts": old_issue_ts,
                        "kind": "issue",
                        "level": "ERROR",
                        "component": "runtime",
                        "event": "old_issue",
                        "status": "error",
                        "summary": "old",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            module.record_activity(event="app_heartbeat", component="runtime", summary="fresh activity")
            module.record_issue(event="gmail_poll_failed", component="gmail", summary="fresh issue")

            activity_records = [json.loads(line) for line in module.OPS_ACTIVITY_PATH.read_text(encoding="utf-8").splitlines()]
            issue_records = [json.loads(line) for line in module.OPS_ISSUES_PATH.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(activity_records), 1)
        self.assertEqual(activity_records[0]["event"], "app_heartbeat")
        self.assertEqual(len(issue_records), 1)
        self.assertEqual(issue_records[0]["event"], "gmail_poll_failed")

    def test_issue_handler_persists_warning_with_operation_id(self):
        module = load_module("tested_opslog_handler", "core/opslog.py")

        with TemporaryDirectory() as td:
            root = Path(td)
            module.OPS_ACTIVITY_PATH = root / "ops_activity.jsonl"
            module.OPS_ISSUES_PATH = root / "ops_issues.jsonl"
            module.OPS_AUDIT_PATH = root / "ops_audit.jsonl"
            module._LAST_PRUNE_AT.clear()

            logger = logging.getLogger("tests.ops")
            logger.handlers = []
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = module.IssuePersistenceHandler()
            logger.addHandler(handler)
            try:
                with module.operation_context("op-test-123"):
                    logger.warning(
                        "Something warning-worthy happened",
                        extra={"ops_event": "telegram_warning", "ops_component": "telegram"},
                    )
            finally:
                logger.removeHandler(handler)

            records = [json.loads(line) for line in module.OPS_ISSUES_PATH.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "telegram_warning")
        self.assertEqual(records[0]["component"], "telegram")
        self.assertEqual(records[0]["op_id"], "op-test-123")


if __name__ == "__main__":
    unittest.main()
