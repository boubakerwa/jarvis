from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
import re


def slugify(value: str) -> str:
    """Return a filesystem-safe slug suitable for Markdown filenames."""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return normalized.strip("-") or "note"


@dataclass
class NoteMatch:
    path: str
    title: str
    snippet: str
    modified_at: str


class ObsidianVault:
    def __init__(self, vault_path: str, root_folder: str = "Marvis"):
        self._vault_path = Path(vault_path).expanduser()
        cleaned_root = root_folder.strip()
        if cleaned_root in {"", ".", "/"}:
            self._root_folder = ""
            self._root_path = self._vault_path
        else:
            self._root_folder = cleaned_root.strip("/ ")
            self._root_path = self._vault_path / self._root_folder
        self._root_path.mkdir(parents=True, exist_ok=True)

    @property
    def root_folder(self) -> str:
        return self._root_folder

    @property
    def root_path(self) -> Path:
        return self._root_path

    def create_note(
        self,
        *,
        folder: str,
        title: str,
        body: str,
        frontmatter: dict[str, object] | None = None,
        slug: str | None = None,
        unique: bool = False,
    ) -> dict:
        directory = self._folder_path(folder)
        directory.mkdir(parents=True, exist_ok=True)
        candidate = directory / f"{slug or slugify(title)}.md"
        path = self._next_available_path(candidate) if unique else candidate
        if path.exists():
            raise FileExistsError(f"Note already exists: {self._display_path(path)}")

        path.write_text(self._compose_text(frontmatter, body), encoding="utf-8")
        return self._note_payload(path)

    def append_to_note(self, display_path: str, content: str) -> dict:
        path = self._resolve_path(display_path)
        if not path.exists():
            raise FileNotFoundError(display_path)

        existing = path.read_text(encoding="utf-8")
        suffix = content.strip()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        if not existing.endswith("\n\n"):
            existing += "\n"
        path.write_text(existing + suffix + "\n", encoding="utf-8")
        return self._note_payload(path)

    def replace_note(
        self,
        display_path: str,
        content: str,
        *,
        preserve_frontmatter: bool = True,
    ) -> dict:
        path = self._resolve_path(display_path)
        if not path.exists():
            raise FileNotFoundError(display_path)

        existing = path.read_text(encoding="utf-8")
        updated = self._normalize_note_text(content)

        if preserve_frontmatter:
            existing_frontmatter, _ = self._split_frontmatter(existing)
            incoming_frontmatter, _ = self._split_frontmatter(updated)
            if existing_frontmatter and not incoming_frontmatter:
                updated = existing_frontmatter + updated.lstrip("\n")

        path.write_text(updated, encoding="utf-8")
        return self._note_payload(path)

    def replace_text_in_note(
        self,
        display_path: str,
        find_text: str,
        replace_with: str,
        *,
        replace_all: bool = False,
    ) -> dict:
        path = self._resolve_path(display_path)
        if not path.exists():
            raise FileNotFoundError(display_path)

        existing = path.read_text(encoding="utf-8")
        match_count = existing.count(find_text)
        if match_count == 0:
            raise ValueError("Could not find the requested text in the note.")
        if match_count > 1 and not replace_all:
            raise ValueError(
                "The requested text appears multiple times. Use replace_all or provide a more specific match."
            )

        updated = existing.replace(find_text, replace_with, -1 if replace_all else 1)
        path.write_text(self._normalize_note_text(updated), encoding="utf-8")
        payload = self._note_payload(path)
        payload["replacement_count"] = match_count if replace_all else 1
        return payload

    def read_note(self, display_path: str, max_chars: int = 8000) -> dict:
        path = self._resolve_path(display_path)
        text = path.read_text(encoding="utf-8")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[...truncated — {len(text) - max_chars} more chars]"
        return {
            "path": self._display_path(path),
            "title": path.stem.replace("-", " ").title(),
            "content": text,
            "modified_at": self._modified_at(path),
        }

    def search_notes(self, query: str, folder: str | None = None, limit: int = 5) -> list[dict]:
        terms = [part for part in re.split(r"\s+", query.lower()) if part]
        if not terms:
            return self.list_recent_notes(folder=folder, limit=limit)

        candidates: list[tuple[int, float, Path, str]] = []
        for path in self._iter_markdown_files(folder):
            text = path.read_text(encoding="utf-8", errors="ignore")
            haystack = f"{path.stem}\n{text}".lower()
            if not all(term in haystack for term in terms):
                continue
            score = sum(haystack.count(term) for term in terms)
            score += 3 * sum(term in path.stem.lower() for term in terms)
            candidates.append((score, path.stat().st_mtime, path, text))

        candidates.sort(key=lambda item: (-item[0], -item[1], item[2].name))
        return [
            self._note_payload(path, snippet=self._snippet(text, terms[0]))
            for _, _, path, text in candidates[: max(1, limit)]
        ]

    def list_recent_notes(self, folder: str | None = None, limit: int = 8) -> list[dict]:
        paths = sorted(
            self._iter_markdown_files(folder),
            key=lambda path: (-path.stat().st_mtime, path.name),
        )
        return [self._note_payload(path) for path in paths[: max(1, limit)]]

    def note_exists(self, display_path: str) -> bool:
        return self._resolve_path(display_path).exists()

    def read_text(self, display_path: str) -> str:
        return self._resolve_path(display_path).read_text(encoding="utf-8")

    def _folder_path(self, folder: str | None) -> Path:
        if not folder:
            return self._root_path
        cleaned = self._clean_relative_path(folder)
        return self._root_path / cleaned

    def _iter_markdown_files(self, folder: str | None = None):
        base = self._folder_path(folder)
        if not base.exists():
            return []
        return [path for path in base.rglob("*.md") if path.is_file()]

    def _resolve_path(self, display_path: str) -> Path:
        cleaned = self._clean_relative_path(display_path)
        if not self._root_folder:
            return self._root_path / cleaned
        if cleaned == self._root_folder:
            return self._root_path
        if cleaned.startswith(f"{self._root_folder}/"):
            cleaned = cleaned[len(self._root_folder) + 1 :]
        return self._root_path / cleaned

    def _display_path(self, path: Path) -> str:
        return str(path.relative_to(self._vault_path)).replace("\\", "/")

    def _modified_at(self, path: Path) -> str:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

    def _note_payload(self, path: Path, snippet: str | None = None) -> dict:
        if snippet is None:
            snippet = self._snippet(path.read_text(encoding="utf-8", errors="ignore"))
        return {
            "path": self._display_path(path),
            "title": path.stem.replace("-", " ").title(),
            "snippet": snippet,
            "modified_at": self._modified_at(path),
        }

    def _compose_text(self, frontmatter: dict[str, object] | None, body: str) -> str:
        if not frontmatter:
            return body.strip() + "\n"
        lines = ["---"]
        for key, value in frontmatter.items():
            if isinstance(value, list):
                rendered = "[" + ", ".join(f'"{item}"' for item in value) + "]"
            elif value is None:
                rendered = "null"
            else:
                rendered = f'"{value}"'
            lines.append(f"{key}: {rendered}")
        lines.append("---")
        lines.append("")
        lines.append(body.strip())
        return "\n".join(lines).rstrip() + "\n"

    def _next_available_path(self, candidate: Path) -> Path:
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        index = 2
        while True:
            next_candidate = candidate.with_name(f"{stem}-{index}{suffix}")
            if not next_candidate.exists():
                return next_candidate
            index += 1

    def _snippet(self, text: str, needle: str | None = None, max_chars: int = 180) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        if not compact:
            return ""
        if needle:
            idx = compact.lower().find(needle.lower())
            if idx != -1:
                start = max(0, idx - 40)
                end = min(len(compact), idx + max_chars - 40)
                excerpt = compact[start:end]
                if start > 0:
                    excerpt = "..." + excerpt
                if end < len(compact):
                    excerpt += "..."
                return excerpt
        return compact[:max_chars] + ("..." if len(compact) > max_chars else "")

    def _split_frontmatter(self, text: str) -> tuple[str, str]:
        normalized = text.replace("\r\n", "\n")
        match = re.match(r"\A---\n.*?\n---(?:\n|$)", normalized, re.DOTALL)
        if not match:
            return "", normalized
        frontmatter = match.group(0).rstrip("\n") + "\n\n"
        body = normalized[match.end():].lstrip("\n")
        return frontmatter, body

    def _normalize_note_text(self, text: str) -> str:
        return text.rstrip() + "\n"

    def _clean_relative_path(self, value: str) -> str:
        cleaned = value.strip().replace("\\", "/").strip("/")
        if not cleaned:
            return ""
        path = PurePosixPath(cleaned)
        if path.is_absolute():
            raise ValueError("Absolute note paths are not allowed.")
        parts = [part for part in path.parts if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise ValueError("Parent directory segments are not allowed in note paths.")
        return "/".join(parts)
