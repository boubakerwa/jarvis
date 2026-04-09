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


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class FakeNotesApi:
    def __init__(self):
        self.created_bodies = []

    def create(self, *, body):
        self.created_bodies.append(body)
        return FakeRequest({"name": "notes/123", "title": body["title"]})


class FakeKeepService:
    def __init__(self):
        self._notes = FakeNotesApi()

    def notes(self):
        return self._notes


class FakeKeepClient:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.deleted = []
        self.created = []

    def find_notes_by_title(self, title):
        return [note for note in self.existing if note.get("title") == title]

    def delete_note(self, name):
        self.deleted.append(name)

    def create_checklist_note(self, title, items):
        self.created.append({"title": title, "items": items})
        return {"name": "notes/fresh", "title": title}


class GoogleKeepClientTests(unittest.TestCase):
    def test_create_checklist_note_uses_list_item_schema(self):
        module = load_module("tested_keep_client_schema", "keep_api/client.py")
        client = module.GoogleKeepClient.__new__(module.GoogleKeepClient)
        client._service = FakeKeepService()

        created = client.create_checklist_note(
            "Marvis Tasks",
            [
                {"text": "Buy milk", "checked": False},
                {"text": "Call the bank", "checked": True},
            ],
        )

        self.assertEqual(created["name"], "notes/123")
        body = client._service.notes().created_bodies[0]
        self.assertEqual(body["title"], "Marvis Tasks")
        self.assertEqual(
            body["body"]["list"]["listItems"],
            [
                {"text": {"text": "Buy milk"}, "checked": False},
                {"text": {"text": "Call the bank"}, "checked": True},
            ],
        )

    def test_task_sync_replaces_existing_note_and_orders_pending_first(self):
        module = load_module("tested_keep_task_sync", "keep_api/client.py")
        fake_client = FakeKeepClient(
            existing=[
                {"name": "notes/old-a", "title": "Marvis Tasks"},
                {"name": "notes/old-b", "title": "Marvis Tasks"},
            ]
        )
        sync = module.GoogleKeepTaskSync(client=fake_client, note_title="Marvis Tasks")

        sync.sync(
            [
                {
                    "id": "task-2",
                    "description": "Done task",
                    "due_date": "",
                    "status": "done",
                    "created_at": "2026-04-09T12:00:00+00:00",
                },
                {
                    "id": "task-1",
                    "description": "Pay electricity bill",
                    "due_date": "2026-04-10",
                    "status": "pending",
                    "created_at": "2026-04-09T09:00:00+00:00",
                },
            ]
        )

        self.assertEqual(fake_client.deleted, ["notes/old-a", "notes/old-b"])
        self.assertEqual(len(fake_client.created), 1)
        created = fake_client.created[0]
        self.assertEqual(created["title"], "Marvis Tasks")
        self.assertEqual(
            created["items"],
            [
                {"text": "Pay electricity bill (due 2026-04-10)", "checked": False},
                {"text": "Done task", "checked": True},
            ],
        )


if __name__ == "__main__":
    unittest.main()
