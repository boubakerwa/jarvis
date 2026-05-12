import importlib.util
import sys
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


class _FakeObservation:
    def __init__(self, store):
        self._store = store

    def update(self, **kwargs):
        self._store.setdefault("updates", []).append(kwargs)
        return self

    def update_trace(self, **kwargs):
        self._store.setdefault("trace_updates", []).append(kwargs)
        return self

    def end(self, **kwargs):
        self._store.setdefault("ends", []).append(kwargs)
        return self


class _FakeObservationContext:
    def __init__(self, store, kwargs):
        self._store = store
        self._kwargs = kwargs

    def __enter__(self):
        self._store["start"] = self._kwargs
        return _FakeObservation(self._store)

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeLangfuseClient:
    def __init__(self):
        self.calls = []
        self.flushed = False

    def start_as_current_observation(self, **kwargs):
        store = {}
        self.calls.append(store)
        return _FakeObservationContext(store, kwargs)

    def flush(self):
        self.flushed = True


class TracingTests(unittest.TestCase):
    def test_start_trace_is_noop_when_disabled(self):
        module = load_module("tested_tracing_noop", "core/tracing.py")
        module.settings.JARVIS_LANGFUSE_ENABLED = False
        module._CLIENT = None
        module._CLIENT_INIT_ATTEMPTED = False

        with module.start_trace(name="chat-turn", input="hello") as observation:
            observation.update(output="world")
            observation.update_trace(metadata={"channel": "chat"})
            observation.end()

        self.assertIs(observation, module.NOOP_OBSERVATION)

    def test_generation_sanitizes_payloads_when_capture_content_is_disabled(self):
        module = load_module("tested_tracing_sanitized", "core/tracing.py")
        client = _FakeLangfuseClient()
        module._CLIENT = client
        module._CLIENT_INIT_ATTEMPTED = True
        module.settings.JARVIS_LANGFUSE_ENABLED = True
        module.settings.JARVIS_LANGFUSE_CAPTURE_CONTENT = False

        with module.start_generation(
            name="chat-generation",
            input="Contact alice@example.com about invoice 123456789",
            metadata={"raw_note": "alice@example.com invoice 123456789"},
            model="anthropic/claude-sonnet-4.6",
        ) as generation:
            generation.update(
                output="Email alice@example.com with invoice 123456789 attached",
                metadata={"tool_result": "invoice 123456789 for alice@example.com"},
            )

        self.assertEqual(len(client.calls), 1)
        start = client.calls[0]["start"]
        self.assertEqual(start["name"], "chat-generation")
        self.assertEqual(start["as_type"], "generation")
        self.assertEqual(start["input"]["label"], "text")
        self.assertGreater(start["input"]["chars"], 20)
        self.assertNotIn("alice@example.com", start["input"]["preview"])
        self.assertNotIn("123456789", start["input"]["preview"])
        self.assertEqual(start["metadata"]["raw_note"]["label"], "text")

        update = client.calls[0]["updates"][0]
        self.assertEqual(update["output"]["label"], "text")
        self.assertNotIn("alice@example.com", update["output"]["preview"])
        self.assertNotIn("123456789", update["output"]["preview"])
        self.assertEqual(update["metadata"]["tool_result"]["label"], "text")

        module.flush()
        self.assertTrue(client.flushed)


if __name__ == "__main__":
    unittest.main()
