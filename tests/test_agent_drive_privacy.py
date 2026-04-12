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


class _DummyMemoryEnum:
    def __init__(self, value=None):
        self.value = value


class _DummyMemoryRecord:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class AgentDrivePrivacyTests(unittest.TestCase):
    def test_read_drive_file_prefers_anonymized_sidecar_when_enabled(self):
        fake_settings = types.SimpleNamespace(MAX_TOKENS=256, JARVIS_ANONYMIZATION_ENABLED=True)
        fake_config = types.ModuleType("config")
        fake_config.settings = fake_settings

        fake_github = types.ModuleType("github_issues")
        fake_github.GitHubAPIError = RuntimeError
        fake_github.GitHubConfigError = RuntimeError
        fake_github.GitHubIssuesClient = object
        fake_github.GitHubTokenMissingError = RuntimeError
        fake_github.load_github_client_config = lambda: None

        fake_core = types.ModuleType("core")
        fake_core.__path__ = []
        fake_log_reader = types.ModuleType("core.log_reader")
        fake_log_reader.read_logs = lambda **_kwargs: []
        fake_llmops = types.ModuleType("core.llmops")
        fake_llmops.record_llm_call = lambda **_kwargs: None
        fake_llm_client = types.ModuleType("core.llm_client")
        fake_llm_client.call_with_free_model_retry = lambda fn, _model: fn()
        fake_llm_client.create_llm_client = lambda: None
        fake_llm_client.get_model_name = lambda *_args, **_kwargs: "unused"
        fake_prompts = types.ModuleType("core.prompts")
        fake_prompts.build_system_prompt = lambda *_args, **_kwargs: "unused"
        fake_source_reader = types.ModuleType("core.source_reader")
        fake_source_reader.read_source_file = lambda _path: {"path": "unused", "content": "", "truncated": False}
        fake_time_utils = types.ModuleType("core.time_utils")
        fake_time_utils.contains_explicit_date = lambda _text: False
        fake_time_utils.day_bounds_for_calendar = lambda *_args, **_kwargs: ("", "")
        fake_time_utils.extract_relative_date_expression = lambda _text: None
        fake_time_utils.get_local_now = lambda: None
        fake_time_utils.resolve_date_expression = lambda *_args, **_kwargs: None
        fake_time_utils.resolve_event_time = lambda *_args, **_kwargs: None

        fake_memory = types.ModuleType("memory")
        fake_memory.__path__ = []
        fake_memory_manager = types.ModuleType("memory.manager")
        fake_memory_manager.MemoryManager = object
        fake_memory_schema = types.ModuleType("memory.schema")
        fake_memory_schema.MemoryCategory = _DummyMemoryEnum
        fake_memory_schema.MemoryConfidence = _DummyMemoryEnum
        fake_memory_schema.MemorySource = _DummyMemoryEnum
        fake_memory_schema.MemoryRecord = _DummyMemoryRecord

        fake_utils = types.ModuleType("utils")
        fake_utils.__path__ = []
        fake_anonymization_store = types.ModuleType("utils.anonymization_store")
        fake_anonymization_store.get_anonymized_document = lambda _file_id: types.SimpleNamespace(
            original_filename="invoice.pdf",
            sanitized_text="Invoice for [PERSON_1]",
        )
        fake_anonymization_store.upsert_anonymized_document = lambda **_kwargs: None

        with unittest.mock.patch.dict(
            sys.modules,
            {
                "config": fake_config,
                "github_issues": fake_github,
                "core": fake_core,
                "core.log_reader": fake_log_reader,
                "core.llmops": fake_llmops,
                "core.llm_client": fake_llm_client,
                "core.prompts": fake_prompts,
                "core.source_reader": fake_source_reader,
                "core.time_utils": fake_time_utils,
                "memory": fake_memory,
                "memory.manager": fake_memory_manager,
                "memory.schema": fake_memory_schema,
                "utils": fake_utils,
                "utils.anonymization_store": fake_anonymization_store,
            },
            clear=False,
        ):
            module = load_module("tested_agent_drive_privacy", "core/agent.py")

        agent = module.JarvisAgent.__new__(module.JarvisAgent)
        module.settings.JARVIS_ANONYMIZATION_ENABLED = True
        agent._drive = types.SimpleNamespace(
            download_file=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("raw Drive download should not be used when an anonymized sidecar exists")
            )
        )

        with unittest.mock.patch.dict(
            sys.modules,
            {"utils.anonymization_store": fake_anonymization_store},
            clear=False,
        ):
            response = agent._tool_read_drive_file({"file_id": "drive-123"})

        self.assertIn("Contents of 'invoice.pdf':", response)
        self.assertIn("[PERSON_1]", response)


if __name__ == "__main__":
    unittest.main()
