import unittest
from unittest.mock import patch

from core import prompts
from memory.schema import MemoryCategory, MemoryConfidence, MemoryRecord, MemorySearchResult, MemorySource


class _FakeMemoryManager:
    def __init__(self, results):
        self._results = results

    def search_scored(self, _query, n_results=8):
        return self._results[:n_results]


def _memory(topic, summary, *, category=MemoryCategory.PREFERENCE, distance=None, document_ref=None):
    return MemorySearchResult(
        record=MemoryRecord(
            topic=topic,
            summary=summary,
            category=category,
            source=MemorySource.TELEGRAM,
            confidence=MemoryConfidence.HIGH,
            document_ref=document_ref,
        ),
        distance=distance,
    )


class PromptTests(unittest.TestCase):
    def test_build_system_prompt_result_caps_default_memories_and_truncates_summaries(self):
        memories = [
            _memory("travel", "Window seat only. " * 20, distance=0.10),
            _memory("coffee", "Prefers light roast coffee.", distance=0.12),
            _memory("gym", "Morning workouts on weekdays.", distance=0.14),
            _memory("noise", "Unrelated note that should stay out.", distance=0.90),
        ]
        manager = _FakeMemoryManager(memories)

        with patch.object(prompts, "get_current_time_context", return_value="Monday, 2026-04-13 09:00 Europe/Berlin"):
            result = prompts.build_system_prompt_result(manager, "What coffee should I buy?")

        self.assertEqual(result.candidate_count, 4)
        self.assertEqual(result.memory_count, 3)
        self.assertEqual(result.memory_topics, ["travel", "coffee", "gym"])
        self.assertIn("Use the provided tool schema as the source of truth", result.prompt)
        self.assertNotIn("Available tools", result.prompt)
        self.assertIn("...", result.prompt)
        self.assertNotIn("Drive ID:", result.prompt)

    def test_build_system_prompt_result_expands_to_high_similarity_matches_and_includes_drive_id_for_documents(self):
        memories = [
            _memory("invoice", "Invoice from Deutsche Bahn", category=MemoryCategory.DOCUMENT_REF, distance=0.10, document_ref="drive-123"),
            _memory("receipt", "Receipt from hotel stay", category=MemoryCategory.DOCUMENT_REF, distance=0.12, document_ref="drive-456"),
            _memory("ticket", "Train ticket PDF", category=MemoryCategory.DOCUMENT_REF, distance=0.14, document_ref="drive-789"),
            _memory("booking", "Flight booking confirmation", category=MemoryCategory.DOCUMENT_REF, distance=0.16, document_ref="drive-222"),
            _memory("contract", "Signed rental contract", category=MemoryCategory.DOCUMENT_REF, distance=0.18, document_ref="drive-333"),
            _memory("noise", "Too far away to include", category=MemoryCategory.DOCUMENT_REF, distance=0.80, document_ref="drive-999"),
        ]
        manager = _FakeMemoryManager(memories)

        with patch.object(prompts, "get_current_time_context", return_value="Monday, 2026-04-13 09:00 Europe/Berlin"):
            result = prompts.build_system_prompt_result(manager, "Find that invoice document from Deutsche Bahn")

        self.assertEqual(result.memory_count, 5)
        self.assertIn("Drive ID: drive-123", result.prompt)
        self.assertIn("Drive ID: drive-333", result.prompt)
        self.assertNotIn("drive-999", result.prompt)


if __name__ == "__main__":
    unittest.main()
