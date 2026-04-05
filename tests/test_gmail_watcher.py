import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class GmailWatcherTests(unittest.TestCase):
    def test_fetch_unread_ids_anchors_to_cutoff_day(self):
        module = load_module("tested_gmail_watcher", "gmail/watcher.py")
        capture = {}

        class FakeMessages:
            def list(self, **kwargs):
                capture["query"] = kwargs["q"]
                return self

            def execute(self):
                return {"messages": [{"id": "msg-1"}]}

        class FakeUsers:
            def messages(self):
                return FakeMessages()

        class FakeService:
            def users(self):
                return FakeUsers()

        with patch.object(module.GmailWatcher, "_build_service", return_value=FakeService()), patch.object(module.GmailWatcher, "_load_state", return_value=("2026-04-05", None)), patch.object(module.GmailWatcher, "_is_on_or_after_cutoff", return_value=True):
            watcher = module.GmailWatcher(on_email=lambda _email: None)
            unread = watcher._fetch_unread_ids()

        self.assertEqual(capture["query"], "is:unread after:2026/04/05")
        self.assertEqual(unread, ["msg-1"])


if __name__ == "__main__":
    unittest.main()
