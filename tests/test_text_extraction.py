import importlib.util
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


class TextExtractionTests(unittest.TestCase):
    def test_extract_pdf_text_prefers_local_ocr_when_pypdf_text_is_weak(self):
        fake_core = types.ModuleType("core")
        fake_core.__path__ = []
        fake_llmops = types.ModuleType("core.llmops")
        fake_llmops.record_llm_call = lambda **_kwargs: None
        fake_llm_client = types.ModuleType("core.llm_client")
        fake_llm_client.create_llm_client = lambda: None
        fake_llm_client.get_model_name = lambda *_args, **_kwargs: "unused"
        fake_opslog = types.ModuleType("core.opslog")
        fake_opslog.record_activity = lambda **_kwargs: None
        fake_opslog.record_issue = lambda **_kwargs: None
        fake_structured_output = types.ModuleType("core.structured_output")
        fake_structured_output.response_text = lambda _response: ""

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "core": fake_core,
                "core.llmops": fake_llmops,
                "core.llm_client": fake_llm_client,
                "core.opslog": fake_opslog,
                "core.structured_output": fake_structured_output,
            },
            clear=False,
        ):
            module = load_module("tested_text_extraction", "utils/text_extraction.py")

        with patch.object(module, "_extract_pdf_text_pypdf", return_value="short"), patch.object(
            module,
            "_ocr_pdf_first_page",
            return_value="This is the OCR fallback text",
        ):
            text = module.extract_text(b"fake-pdf", "application/pdf", "scan.pdf")

        self.assertEqual(text, "This is the OCR fallback text")


if __name__ == "__main__":
    unittest.main()
