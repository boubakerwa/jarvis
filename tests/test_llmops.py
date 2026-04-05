import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class LLMOpsTests(unittest.TestCase):
    def test_record_llm_call_persists_usage_and_estimated_cost(self):
        module = load_module("tested_llmops", "core/llmops.py")

        with TemporaryDirectory() as td:
            temp_path = Path(td) / "llm_activity.jsonl"
            module.LLM_ACTIVITY_PATH = temp_path
            response = SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=1000,
                    output_tokens=200,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
                stop_reason="end_turn",
            )

            module.record_llm_call(
                task="chat",
                model="anthropic/claude-sonnet-4.6",
                status="ok",
                started_at="2026-04-05T15:00:00+00:00",
                latency_ms=812.34,
                response=response,
                metadata={"channel": "chat", "tool_use_count": 1},
            )

            payload = json.loads(temp_path.read_text(encoding="utf-8").strip())

        self.assertEqual(payload["task"], "chat")
        self.assertEqual(payload["model"], "anthropic/claude-sonnet-4.6")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["input_tokens"], 1000)
        self.assertEqual(payload["output_tokens"], 200)
        self.assertEqual(payload["total_tokens"], 1200)
        self.assertEqual(payload["stop_reason"], "end_turn")
        self.assertAlmostEqual(payload["estimated_cost_usd"], 0.006)
        self.assertEqual(payload["metadata"]["tool_use_count"], 1)


if __name__ == "__main__":
    unittest.main()
