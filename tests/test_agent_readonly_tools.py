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


class AgentReadOnlyToolTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module("tested_agent_readonly_tools", "core/agent.py")

    def test_read_source_file_tool_formats_contents(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        self.module.read_project_source_file = lambda path: {
            "path": "core/agent.py",
            "content": "print('hi')",
            "truncated": False,
        }

        response = agent._tool_read_source_file({"path": "core/agent.py"})

        self.assertIn("Contents of core/agent.py:", response)
        self.assertIn("print('hi')", response)

    def test_read_logs_tool_formats_entries(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        self.module.query_logs = lambda **_kwargs: [
            {
                "ts": "2026-04-10T12:00:00+00:00",
                "level": "ERROR",
                "component": "reminders",
                "event": "reminder_send_failed",
                "summary": "Reminder failed",
                "source": "ops_issues",
                "status": "warning",
                "op_id": "reminder-123",
                "metadata": {"reminder_id": "abc"},
            }
        ]

        response = agent._tool_read_logs({"level": "ERROR", "limit": 5})

        self.assertIn("reminders::reminder_send_failed", response)
        self.assertIn("Reminder failed", response)
        self.assertIn("metadata=", response)

    def test_read_source_file_tool_surfaces_sandbox_errors(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)

        def _raise(_path):
            raise ValueError("Path must stay within the Marvis project root")

        self.module.read_project_source_file = _raise

        response = agent._tool_read_source_file({"path": "../secrets.txt"})

        self.assertIn("Could not read source file", response)


if __name__ == "__main__":
    unittest.main()
