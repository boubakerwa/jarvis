import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class MainEmailNotificationTests(unittest.TestCase):
    def test_format_email_summary_message_includes_reason_and_counts(self):
        module = load_module("tested_main_email_notifications", "main.py")
        email = SimpleNamespace(
            subject="April invoice",
            sender="Vendor <billing@example.com>",
        )
        result = module.EmailProcessingResult(
            outcome="partial",
            reason="Worth filing because it looks like a real invoice",
            filed_count=1,
            failed_count=2,
        )

        summary = module._format_email_summary_message(email, result)

        self.assertIn("[Gmail] Email partial", summary)
        self.assertIn("Subject: April invoice", summary)
        self.assertIn("From: Vendor <billing@example.com>", summary)
        self.assertIn("Reason: Worth filing because it looks like a real invoice", summary)
        self.assertIn("Attachments: 1 filed, 2 failed", summary)

    def test_format_email_failure_message_trims_long_error_text(self):
        module = load_module("tested_main_email_failures", "main.py")
        email = SimpleNamespace(
            subject="Quarterly statements",
            sender="Bank <alerts@example.com>",
        )
        error = RuntimeError("x" * 250)

        summary = module._format_email_failure_message(email, error)

        self.assertIn("[Gmail] Email processing error", summary)
        self.assertIn("Subject: Quarterly statements", summary)
        self.assertIn("From: Bank <alerts@example.com>", summary)
        self.assertIn("Error: ", summary)
        self.assertIn("...", summary)


if __name__ == "__main__":
    unittest.main()
