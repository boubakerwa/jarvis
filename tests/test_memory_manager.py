import importlib.util
import sqlite3
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


class FakeTaskSync:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.calls = []

    def sync(self, tasks):
        self.calls.append([dict(task) for task in tasks])
        if self.should_fail:
            raise RuntimeError("sync failed")


class MemoryManagerTaskSyncTests(unittest.TestCase):
    def _build_manager(self, module, task_sync):
        manager = module.MemoryManager.__new__(module.MemoryManager)
        manager._db = sqlite3.connect(":memory:")
        manager._db.row_factory = sqlite3.Row
        manager._db.executescript(module._CREATE_TASKS_TABLE)
        manager._task_sync = task_sync
        return manager

    def test_create_and_complete_task_sync_google_keep_without_changing_local_behavior(self):
        module = load_module("tested_memory_manager_sync", "memory/manager.py")
        fake_sync = FakeTaskSync()
        manager = self._build_manager(module, fake_sync)

        created = module.MemoryManager.create_task(manager, "Book dentist appointment", "2026-04-15")
        completed = module.MemoryManager.complete_task(manager, created["id"])
        tasks = module.MemoryManager.list_tasks(manager, "all")

        self.assertTrue(completed)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "done")
        self.assertEqual(len(fake_sync.calls), 2)
        self.assertEqual(fake_sync.calls[0][0]["description"], "Book dentist appointment")
        self.assertEqual(fake_sync.calls[1][0]["status"], "done")

    def test_task_sync_failure_does_not_block_local_task_creation(self):
        module = load_module("tested_memory_manager_sync_failure", "memory/manager.py")
        fake_sync = FakeTaskSync(should_fail=True)
        manager = self._build_manager(module, fake_sync)

        created = module.MemoryManager.create_task(manager, "Pick up package")
        tasks = module.MemoryManager.list_tasks(manager, "pending")

        self.assertEqual(created["description"], "Pick up package")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["description"], "Pick up package")
        self.assertEqual(len(fake_sync.calls), 1)


if __name__ == "__main__":
    unittest.main()
