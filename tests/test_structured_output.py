import importlib.util
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _DummyObservation:
    def update(self, **_kwargs):
        return self

    def end(self, **_kwargs):
        return self


@contextmanager
def _dummy_context_manager(**_kwargs):
    yield _DummyObservation()


class StructuredOutputTests(unittest.TestCase):
    def test_extract_json_object_handles_code_fences(self):
        fake_llm_client = types.ModuleType("core.llm_client")
        fake_llm_client.create_llm_client = lambda: None
        fake_llm_client.get_model_candidates = lambda *_args, **_kwargs: []
        fake_llm_client.call_with_free_model_retry = lambda fn, _model: fn()

        fake_core = types.ModuleType("core")
        fake_core.__path__ = []
        fake_llmops = types.ModuleType("core.llmops")
        fake_llmops.record_llm_call = lambda **_kwargs: None
        fake_tracing = types.ModuleType("core.tracing")
        fake_tracing.generation_cost_details = lambda *_args, **_kwargs: None
        fake_tracing.generation_usage_details = lambda *_args, **_kwargs: {}
        fake_tracing.start_generation = _dummy_context_manager

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "core": fake_core,
                "core.llm_client": fake_llm_client,
                "core.llmops": fake_llmops,
                "core.tracing": fake_tracing,
            },
            clear=False,
        ):
            module = load_module("tested_structured_output", "core/structured_output.py")

        parsed = module.extract_json_object(
            "Here you go:\n```json\n{\"vendor\": \"Vodafone\", \"amount\": 29.99}\n```\nThanks."
        )
        self.assertEqual(parsed["vendor"], "Vodafone")
        self.assertEqual(parsed["amount"], 29.99)


if __name__ == "__main__":
    unittest.main()
