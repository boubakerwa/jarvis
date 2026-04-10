import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notes import NotesManager, ObsidianVault


class NotesServiceTests(unittest.TestCase):
    def test_create_note_can_write_at_vault_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = NotesManager(ObsidianVault(tmpdir, root_folder="."))

            note = manager.create_note(title="Root note", body="# Root note", folder="Ideas")

            note_path = Path(tmpdir) / note["path"]
            self.assertTrue(note_path.exists())
            self.assertEqual(note["path"], "Ideas/root-note.md")

    def test_create_note_writes_to_requested_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = NotesManager(ObsidianVault(tmpdir))

            note = manager.create_note(
                title="Gift ideas for Anna",
                body="# Gift ideas for Anna\n\n- [ ] Espresso machine",
                folder="Personal/Gifts",
                tags=["gifts", "anna"],
                note_type="idea_list",
            )

            note_path = Path(tmpdir) / note["path"]
            content = note_path.read_text(encoding="utf-8")
            self.assertTrue(note_path.exists())
            self.assertIn("Personal/Gifts", note["path"])
            self.assertIn('type: "idea_list"', content)
            self.assertIn("Espresso machine", content)

    def test_create_note_can_generate_unique_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = NotesManager(ObsidianVault(tmpdir))

            first = manager.create_note(title="Weekly review", folder="Planning", unique=True)
            second = manager.create_note(title="Weekly review", folder="Planning", unique=True)

            self.assertNotEqual(first["path"], second["path"])
            self.assertTrue(second["path"].endswith("weekly-review-2.md"))

    def test_append_note_updates_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = NotesManager(ObsidianVault(tmpdir))
            created = manager.create_note(title="Project alpha", body="# Project alpha", folder="Projects")

            manager.append_note(created["path"], "## Next step\nShip the first prototype.")

            note_path = Path(tmpdir) / created["path"]
            content = note_path.read_text(encoding="utf-8")
            self.assertIn("## Next step", content)
            self.assertIn("Ship the first prototype.", content)

    def test_update_note_can_replace_exact_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = NotesManager(ObsidianVault(tmpdir))
            created = manager.create_note(
                title="Project alpha",
                body="# Project alpha\n\nStatus: Draft",
                folder="Projects",
            )

            result = manager.update_note(
                created["path"],
                find_text="Status: Draft",
                replace_with="Status: Ready",
            )

            note_path = Path(tmpdir) / created["path"]
            content = note_path.read_text(encoding="utf-8")
            self.assertEqual(result["mode"], "replace_text")
            self.assertIn("Status: Ready", content)
            self.assertNotIn("Status: Draft", content)

    def test_update_note_can_replace_full_content_and_keep_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = NotesManager(ObsidianVault(tmpdir))
            created = manager.create_note(
                title="Project alpha",
                body="# Project alpha\n\nOld body",
                folder="Projects",
                tags=["active"],
                note_type="project",
            )

            result = manager.update_note(
                created["path"],
                content="# Project alpha\n\nNew body",
            )

            note_path = Path(tmpdir) / created["path"]
            content = note_path.read_text(encoding="utf-8")
            self.assertEqual(result["mode"], "replace_content")
            self.assertIn('type: "project"', content)
            self.assertIn('tags: ["active"]', content)
            self.assertIn("New body", content)
            self.assertNotIn("Old body", content)

    def test_update_note_falls_back_to_atomic_replace_when_in_place_write_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = NotesManager(ObsidianVault(tmpdir, root_folder="."))
            created = manager.create_note(
                title="Project alpha",
                body="# Project alpha\n\nOld body",
                folder="Projects",
            )

            note_path = Path(tmpdir) / created["path"]
            original_write_text = Path.write_text

            def flaky_write_text(path_self, data, *args, **kwargs):
                if path_self == note_path:
                    raise PermissionError(1, "Operation not permitted", str(path_self))
                return original_write_text(path_self, data, *args, **kwargs)

            with mock.patch.object(Path, "write_text", autospec=True, side_effect=flaky_write_text):
                result = manager.update_note(
                    created["path"],
                    content="# Project alpha\n\nNew body",
                    preserve_frontmatter=False,
                )

            content = note_path.read_text(encoding="utf-8")
            self.assertEqual(result["mode"], "replace_content")
            self.assertIn("New body", content)
            self.assertNotIn("Old body", content)

    def test_search_notes_matches_filename_and_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = NotesManager(ObsidianVault(tmpdir))
            manager.create_note(title="Hot project", body="This should rank highly", folder="Ideas")
            manager.create_note(title="Cool article", body="Talks about hot project momentum", folder="Writing")

            matches = manager.search_notes("hot project", limit=2)

            self.assertGreaterEqual(len(matches), 1)
            self.assertIn("hot-project.md", matches[0]["path"])


if __name__ == "__main__":
    unittest.main()
