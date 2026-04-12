import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FilerPrivacyTests(unittest.TestCase):
    def test_classify_attachment_rejects_remote_image_vision_when_anonymization_enabled(self):
        fake_settings = types.SimpleNamespace(JARVIS_ANONYMIZATION_ENABLED=True)
        fake_config = types.ModuleType("config")
        fake_config.settings = fake_settings

        fake_core = types.ModuleType("core")
        fake_core.__path__ = []
        fake_structured_output = types.ModuleType("core.structured_output")
        fake_structured_output.generate_validated_json = lambda **_kwargs: None

        fake_storage = types.ModuleType("storage")
        fake_storage.__path__ = []
        fake_schema = types.ModuleType("storage.schema")
        fake_schema.TOP_LEVEL_FOLDERS = ["Misc"]
        fake_schema.build_classification_prompt = lambda *_args, **_kwargs: "unused"

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "config": fake_config,
                "core": fake_core,
                "core.structured_output": fake_structured_output,
                "storage": fake_storage,
                "storage.schema": fake_schema,
            },
            clear=False,
        ):
            module = load_module("tested_filer_privacy", "agent_sdk/filer.py")

        with self.assertRaises(ValueError):
            module.classify_attachment(
                "photo.jpg",
                "image/jpeg",
                "",
                raw_data=b"fake-image-bytes",
            )

    def test_classify_attachment_locally_routes_finance_documents(self):
        fake_settings = types.SimpleNamespace(JARVIS_ANONYMIZATION_ENABLED=True)
        fake_config = types.ModuleType("config")
        fake_config.settings = fake_settings

        fake_core = types.ModuleType("core")
        fake_core.__path__ = []
        fake_structured_output = types.ModuleType("core.structured_output")
        fake_structured_output.generate_validated_json = lambda **_kwargs: None

        fake_storage = types.ModuleType("storage")
        fake_storage.__path__ = []
        fake_schema = types.ModuleType("storage.schema")
        fake_schema.TOP_LEVEL_FOLDERS = ["Finances", "Misc"]
        fake_schema.build_classification_prompt = lambda *_args, **_kwargs: "unused"

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "config": fake_config,
                "core": fake_core,
                "core.structured_output": fake_structured_output,
                "storage": fake_storage,
                "storage.schema": fake_schema,
            },
            clear=False,
        ):
            module = load_module("tested_filer_local_classification", "agent_sdk/filer.py")

        result = module.classify_attachment_locally(
            "steuerbescheid_2025.pdf",
            "application/pdf",
            "Steuerbescheid Finanzamt 2025",
            summary_reason="local anonymization timed out",
        )

        self.assertEqual(result.top_level, "Finances")
        self.assertEqual(result.sub_folder, "Tax")
        self.assertIn("local anonymization timed out", result.summary)


if __name__ == "__main__":
    unittest.main()
