from __future__ import annotations

from datetime import datetime, timezone

from notes.obsidian import ObsidianVault, slugify


class NotesManager:
    def __init__(self, vault: ObsidianVault):
        self._vault = vault

    def create_note(
        self,
        title: str,
        body: str = "",
        *,
        folder: str = "",
        tags: list[str] | None = None,
        note_type: str = "",
        unique: bool = False,
    ) -> dict:
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("Note title cannot be empty.")
        note_body = body.strip() or f"# {cleaned_title}\n"
        frontmatter = {
            "created_at": self._timestamp(),
        }
        if tags:
            frontmatter["tags"] = tags
        if note_type.strip():
            frontmatter["type"] = note_type.strip()
        note = self._vault.create_note(
            folder=folder,
            title=cleaned_title,
            slug=slugify(cleaned_title),
            unique=unique,
            frontmatter=frontmatter,
            body=note_body,
        )
        return {"path": note["path"], "title": cleaned_title}

    def append_note(self, path: str, content: str) -> dict:
        if not content.strip():
            raise ValueError("Appended note content cannot be empty.")
        note = self._vault.append_to_note(path, content.strip())
        return {"path": note["path"]}

    def search_notes(self, query: str, folder: str | None = None, limit: int = 5) -> list[dict]:
        return self._vault.search_notes(query, folder=folder, limit=limit)

    def read_note(self, path: str, max_chars: int = 8000) -> dict:
        return self._vault.read_note(path, max_chars=max_chars)

    def list_recent_notes(self, folder: str | None = None, limit: int = 8) -> list[dict]:
        return self._vault.list_recent_notes(folder=folder, limit=limit)

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
