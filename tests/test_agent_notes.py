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


class FakeNotes:
    def create_note(self, title, body="", folder="", tags=None, note_type="", unique=False):
        return {"path": "Marvis/Ideas/gift-ideas-for-anna.md", "title": title}

    def append_note(self, path, content):
        return {"path": path}

    def search_notes(self, query, folder=None, limit=5):
        return [
            {
                "path": "Marvis/Ideas/local-first.md",
                "snippet": "A local-first project idea",
                "modified_at": "2026-04-05T12:00:00+00:00",
            }
        ]

    def read_note(self, path, max_chars=8000):
        return {"path": path, "content": "# Example", "modified_at": "2026-04-05T12:00:00+00:00"}

    def list_recent_notes(self, folder=None, limit=8):
        return [
            {
                "path": "Marvis/Ideas/local-first.md",
                "snippet": "Recent update",
                "modified_at": "2026-04-05T12:00:00+00:00",
            }
        ]


class AgentNotesTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module("tested_agent_notes", "core/agent.py")

    def test_create_note_returns_path(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._notes = FakeNotes()

        response = agent._tool_create_note(
            {
                "title": "Gift ideas for Anna",
                "body": "# Gift ideas for Anna\n\n- [ ] Espresso machine",
                "folder": "Ideas",
            }
        )

        self.assertIn("Note created", response)
        self.assertIn("Marvis/Ideas/gift-ideas-for-anna.md", response)

    def test_append_note_returns_updated_path(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._notes = FakeNotes()

        response = agent._tool_append_note(
            {
                "path": "Marvis/Ideas/gift-ideas-for-anna.md",
                "content": "- [ ] Weekend bag",
            }
        )

        self.assertIn("Note updated", response)

    def test_list_recent_notes_returns_formatted_results(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._notes = FakeNotes()

        response = agent._tool_list_recent_notes({"limit": 5})

        self.assertIn("Marvis/Ideas/local-first.md", response)
        self.assertIn("Recent update", response)


if __name__ == "__main__":
    unittest.main()
