import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def text_block(text: str):
    return types.SimpleNamespace(type="text", text=text)


def tool_use_block(tool_id: str, name: str, payload: dict):
    return types.SimpleNamespace(type="tool_use", id=tool_id, name=name, input=payload)


class _DummyMemoryEnum:
    def __init__(self, value=None):
        self.value = value


class _DummyMemoryRecord:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class AgentLoopTests(unittest.TestCase):
    def test_run_loop_round_trips_tool_results_in_anthropic_format(self):
        fake_settings = types.SimpleNamespace(MAX_TOKENS=256)
        fake_config = types.ModuleType("config")
        fake_config.settings = fake_settings

        fake_core_package = types.ModuleType("core")
        fake_core_package.__path__ = []
        fake_prompts = types.ModuleType("core.prompts")
        fake_prompts.build_system_prompt = lambda *_args, **_kwargs: "unused"

        fake_memory_package = types.ModuleType("memory")
        fake_memory_package.__path__ = []
        fake_memory_manager = types.ModuleType("memory.manager")
        fake_memory_manager.MemoryManager = object
        fake_memory_schema = types.ModuleType("memory.schema")
        fake_memory_schema.MemoryCategory = _DummyMemoryEnum
        fake_memory_schema.MemoryConfidence = _DummyMemoryEnum
        fake_memory_schema.MemorySource = _DummyMemoryEnum
        fake_memory_schema.MemoryRecord = _DummyMemoryRecord

        response1 = types.SimpleNamespace(
            stop_reason="tool_use",
            content=[
                text_block("Looking that up."),
                tool_use_block("tool_1", "recall", {"query": "travel preferences"}),
            ],
        )
        response2 = types.SimpleNamespace(
            stop_reason="end_turn",
            content=[text_block("Done.")],
        )

        create_mock = Mock(side_effect=[response1, response2])
        fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create_mock))
        fake_llm_client = types.ModuleType("core.llm_client")
        fake_llm_client.call_with_free_model_retry = lambda fn, _model_name: fn()
        fake_llm_client.create_llm_client = Mock(return_value=fake_client)
        fake_llm_client.get_model_name = Mock(return_value="anthropic/claude-sonnet-4.6")

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "config": fake_config,
                "core": fake_core_package,
                "core.prompts": fake_prompts,
                "core.llm_client": fake_llm_client,
                "memory": fake_memory_package,
                "memory.manager": fake_memory_manager,
                "memory.schema": fake_memory_schema,
            },
            clear=False,
        ):
            module = load_module("tested_agent", "core/agent.py")
            agent = module.JarvisAgent(memory_manager=object())
            agent._history = [{"role": "user", "content": "hello"}]
            agent._execute_tool = Mock(return_value="tool-output")

            result = agent._run_loop("system prompt")

        self.assertEqual(result, "Done.")
        self.assertEqual(create_mock.call_count, 2)
        first_call = create_mock.call_args_list[0].kwargs
        self.assertEqual(first_call["model"], "anthropic/claude-sonnet-4.6")
        self.assertEqual(first_call["messages"][0], {"role": "user", "content": "hello"})

        second_messages = create_mock.call_args_list[1].kwargs["messages"]
        self.assertEqual(second_messages[1]["role"], "assistant")
        self.assertEqual(second_messages[2]["role"], "user")
        tool_result = second_messages[2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "tool_1")
        self.assertEqual(tool_result["content"], "tool-output")


if __name__ == "__main__":
    unittest.main()
