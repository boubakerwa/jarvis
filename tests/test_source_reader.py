import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.source_reader import read_source_file, resolve_project_path


class SourceReaderTests(unittest.TestCase):
    def test_read_source_file_within_project_root(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            target = root / "core" / "example.py"
            target.parent.mkdir(parents=True)
            target.write_text("print('hello')\n", encoding="utf-8")

            result = read_source_file("core/example.py", root=root)

        self.assertEqual(result["path"], "core/example.py")
        self.assertEqual(result["content"], "print('hello')\n")
        self.assertFalse(result["truncated"])

    def test_resolve_project_path_rejects_traversal(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "core").mkdir()

            with self.assertRaises(ValueError):
                resolve_project_path("../secret.txt", root=root)

    def test_read_source_file_rejects_binary_files(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            target = root / "data.bin"
            target.write_bytes(b"\x00\x01\x02")

            with self.assertRaises(ValueError):
                read_source_file("data.bin", root=root)

    def test_read_source_file_supports_line_ranges(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            target = root / "core" / "example.py"
            target.parent.mkdir(parents=True)
            target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")

            result = read_source_file("core/example.py", root=root, start_line=2, end_line=4, max_chars=1000)

        self.assertEqual(result["content"], "two\nthree\nfour")
        self.assertEqual(result["start_line"], 2)
        self.assertEqual(result["end_line"], 4)
        self.assertEqual(result["total_lines"], 5)
        self.assertFalse(result["truncated"])


if __name__ == "__main__":
    unittest.main()
