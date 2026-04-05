import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class LLMClientTests(unittest.TestCase):
    def test_create_llm_client_uses_openrouter_auth(self):
        fake_anthropic = types.ModuleType("anthropic")
        fake_constructor = Mock(return_value="client")
        fake_anthropic.Anthropic = fake_constructor

        fake_settings = types.SimpleNamespace(
            OPENROUTER_API_KEY="test-openrouter-key",
            OPENROUTER_BASE_URL="https://openrouter.ai/api",
            OPENROUTER_MODEL="anthropic/claude-sonnet-4.6",
            OPENROUTER_MODEL_RELEVANCE="google/gemma-4-31b-it",
            OPENROUTER_MODEL_FINANCIAL=None,
            OPENROUTER_MODEL_CLASSIFICATION=None,
            OPENROUTER_MODEL_VISION=None,
        )
        fake_config = types.ModuleType("config")
        fake_config.settings = fake_settings

        with patch.dict(sys.modules, {"anthropic": fake_anthropic, "config": fake_config}, clear=False):
            module = load_module("tested_llm_client", "core/llm_client.py")
            os.environ["ANTHROPIC_API_KEY"] = "legacy-key"
            try:
                client = module.create_llm_client()
                self.assertEqual(client, "client")
                self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
                fake_constructor.assert_called_once_with(
                    auth_token="test-openrouter-key",
                    base_url="https://openrouter.ai/api",
                )
                self.assertEqual(module.get_model_name(), "anthropic/claude-sonnet-4.6")
                self.assertEqual(module.get_model_name("relevance"), "google/gemma-4-31b-it")
                self.assertEqual(
                    module.get_model_candidates("relevance"),
                    ["google/gemma-4-31b-it", "anthropic/claude-sonnet-4.6"],
                )
            finally:
                os.environ.pop("ANTHROPIC_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
