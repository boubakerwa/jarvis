import importlib.util
import sys
import types
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


class AnonymizationStoreTests(unittest.TestCase):
    def test_upsert_and_get_round_trip(self):
        with TemporaryDirectory() as td:
            db_path = str(Path(td) / "jarvis.db")
            fake_settings = types.SimpleNamespace(JARVIS_DB_PATH=db_path)
            fake_config = types.ModuleType("config")
            fake_config.settings = fake_settings

            with unittest.mock.patch.dict(
                sys.modules,
                {"config": fake_config},
                clear=False,
            ):
                module = load_module("tested_anonymization_store", "utils/anonymization_store.py")

            module.upsert_anonymized_document(
                drive_file_id="drive-123",
                content_sha256="abc123",
                original_filename="invoice.pdf",
                mime_type="application/pdf",
                sanitized_text="Invoice for [PERSON_1]",
                backend="ollama",
                model="gemma3:12b",
                replacement_counts={"PERSON": 1},
                truncated=False,
            )
            stored = module.get_anonymized_document("drive-123")

        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.original_filename, "invoice.pdf")
        self.assertEqual(stored.sanitized_text, "Invoice for [PERSON_1]")
        self.assertEqual(stored.replacement_counts["PERSON"], 1)


if __name__ == "__main__":
    unittest.main()
