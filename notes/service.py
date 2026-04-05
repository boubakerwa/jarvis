from __future__ import annotations

from datetime import datetime, timezone

from core.opslog import record_audit
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
        record_audit(
            event="note_created",
            component="notes",
            summary="Created note in shared workspace",
            metadata={"folder": folder or "", "path": note["path"]},
        )
        return {"path": note["path"], "title": cleaned_title}

    def append_note(self, path: str, content: str) -> dict:
        if not content.strip():
            raise ValueError("Appended note content cannot be empty.")
        note = self._vault.append_to_note(path, content.strip())
        record_audit(
            event="note_appended",
            component="notes",
            summary="Appended content to note",
            metadata={"path": note["path"]},
        )
        return {"path": note["path"]}

    def update_note(
        self,
        path: str,
        *,
        content: str | None = None,
        find_text: str | None = None,
        replace_with: str | None = None,
        replace_all: bool = False,
        preserve_frontmatter: bool = True,
    ) -> dict:
        if content is not None and (find_text is not None or replace_with is not None):
            raise ValueError("Choose either full note replacement or exact text replacement, not both.")

        if content is not None:
            if not content.strip():
                raise ValueError("Updated note content cannot be empty.")
            note = self._vault.replace_note(
                path,
                content.strip(),
                preserve_frontmatter=preserve_frontmatter,
            )
            record_audit(
                event="note_updated",
                component="notes",
                summary="Replaced note content",
                metadata={"path": note["path"], "mode": "replace_content"},
            )
            return {"path": note["path"], "mode": "replace_content"}

        if not find_text:
            raise ValueError("find_text is required when replacing text within a note.")
        if replace_with is None:
            raise ValueError("replace_with is required when replacing text within a note.")

        note = self._vault.replace_text_in_note(
            path,
            find_text,
            replace_with,
            replace_all=replace_all,
        )
        record_audit(
            event="note_updated",
            component="notes",
            summary="Updated note text",
            metadata={
                "path": note["path"],
                "mode": "replace_text",
                "replacement_count": note["replacement_count"],
            },
        )
        return {
            "path": note["path"],
            "mode": "replace_text",
            "replacement_count": note["replacement_count"],
        }

    def search_notes(self, query: str, folder: str | None = None, limit: int = 5) -> list[dict]:
        return self._vault.search_notes(query, folder=folder, limit=limit)

    def read_note(self, path: str, max_chars: int = 8000) -> dict:
        return self._vault.read_note(path, max_chars=max_chars)

    def list_recent_notes(self, folder: str | None = None, limit: int = 8) -> list[dict]:
        return self._vault.list_recent_notes(folder=folder, limit=limit)

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
