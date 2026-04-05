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
    spec.loader.exec_module(module)
    return module


class FinancialExtractionTests(unittest.TestCase):
    def test_validate_financial_payload_normalizes_gemma_style_values(self):
        fake_structured_output = types.ModuleType("core.structured_output")

        class _StructuredOutputError(Exception):
            pass

        fake_structured_output.StructuredOutputError = _StructuredOutputError
        fake_structured_output.generate_validated_json = lambda **_kwargs: None

        fake_core = types.ModuleType("core")
        fake_core.__path__ = []

        with unittest.mock.patch.dict(
            sys.modules,
            {"core": fake_core, "core.structured_output": fake_structured_output},
            clear=False,
        ):
            module = load_module("tested_financial_extraction", "utils/financial_extraction.py")

        result = module._validate_financial_payload(
            {
                "vendor": "Vodafone GmbH",
                "amount": "29,99 EUR",
                "currency": "eur",
                "date": "02.03.2026",
                "category": "Internet",
            }
        )
        self.assertEqual(result["vendor"], "Vodafone GmbH")
        self.assertEqual(result["amount"], 29.99)
        self.assertEqual(result["currency"], "EUR")
        self.assertEqual(result["date"], "2026-03-02")
        self.assertEqual(result["category"], "subscription")


if __name__ == "__main__":
    unittest.main()
