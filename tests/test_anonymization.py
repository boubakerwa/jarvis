import importlib.util
import json
import sys
import types
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


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class AnonymizationTests(unittest.TestCase):
    def test_anonymize_text_masks_known_patterns_and_uses_local_model(self):
        fake_settings = types.SimpleNamespace(
            JARVIS_ANONYMIZATION_ENABLED=True,
            JARVIS_ANONYMIZATION_FAIL_CLOSED=True,
            JARVIS_ANONYMIZATION_MAX_CHARS=12000,
            OLLAMA_MODEL_ANONYMIZER="gemma3:12b",
            OLLAMA_BASE_URL="http://127.0.0.1:11434",
            OLLAMA_TIMEOUT_SECONDS=30,
        )
        fake_config = types.ModuleType("config")
        fake_config.settings = fake_settings

        fake_core = types.ModuleType("core")
        fake_core.__path__ = []
        fake_opslog = types.ModuleType("core.opslog")
        fake_opslog.record_activity = lambda **_kwargs: None
        fake_opslog.record_issue = lambda **_kwargs: None

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "config": fake_config,
                "core": fake_core,
                "core.opslog": fake_opslog,
            },
            clear=False,
        ):
            module = load_module("tested_anonymization", "utils/anonymization.py")

        text = (
            "Invoice for John Doe\n"
            "Email: john@example.com\n"
            "Phone: +49 123 456789\n"
            "Customer ID: 12345678\n"
        )
        local_output = {
            "response": (
                "Invoice for [PERSON_1]\n"
                "Email: [EMAIL_1]\n"
                "Phone: [PHONE_1]\n"
                "Customer ID: [ID_1]\n"
            )
        }

        with patch.object(module.urllib_request, "urlopen", return_value=_FakeHTTPResponse(local_output)):
            result = module.anonymize_text(text, filename="invoice.pdf", mime_type="application/pdf")

        self.assertEqual(result.backend, "ollama")
        self.assertEqual(result.model, "gemma3:12b")
        self.assertIn("[PERSON_1]", result.sanitized_text)
        self.assertIn("[EMAIL_1]", result.sanitized_text)
        self.assertIn("[PHONE_1]", result.sanitized_text)
        self.assertIn("[ID_1]", result.sanitized_text)
        self.assertGreaterEqual(result.replacement_counts["EMAIL"], 1)
        self.assertTrue(result.changed)

    def test_prepare_text_for_remote_processing_requests_manual_review_when_text_missing(self):
        fake_settings = types.SimpleNamespace(
            JARVIS_ANONYMIZATION_ENABLED=True,
            JARVIS_ANONYMIZATION_FAIL_CLOSED=True,
            JARVIS_ANONYMIZATION_MAX_CHARS=12000,
            OLLAMA_MODEL_ANONYMIZER="gemma3:12b",
            OLLAMA_BASE_URL="http://127.0.0.1:11434",
            OLLAMA_TIMEOUT_SECONDS=30,
        )
        fake_config = types.ModuleType("config")
        fake_config.settings = fake_settings

        fake_core = types.ModuleType("core")
        fake_core.__path__ = []
        fake_opslog = types.ModuleType("core.opslog")
        fake_opslog.record_activity = lambda **_kwargs: None
        fake_opslog.record_issue = lambda **_kwargs: None

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "config": fake_config,
                "core": fake_core,
                "core.opslog": fake_opslog,
            },
            clear=False,
        ):
            module = load_module("tested_anonymization_prepare", "utils/anonymization.py")

        text, result, review_reason = module.prepare_text_for_remote_processing(
            "",
            filename="scan.pdf",
            mime_type="application/pdf",
        )

        self.assertEqual(text, "")
        self.assertIsNone(result)
        self.assertIn("anonymization-safe text", review_reason)


if __name__ == "__main__":
    unittest.main()
