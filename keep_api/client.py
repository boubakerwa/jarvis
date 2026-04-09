from __future__ import annotations

import logging
import os
from time import monotonic

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import settings
from core.opslog import record_activity, record_audit, record_issue

logger = logging.getLogger(__name__)


class GoogleKeepClient:
    def __init__(self):
        self._service = self._build_service()

    def _build_service(self):
        creds = None
        token_path = settings.GOOGLE_TOKEN_PATH
        creds_path = settings.GOOGLE_CREDENTIALS_PATH

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, settings.GOOGLE_SCOPES)

        missing_scopes = bool(creds and not creds.has_scopes(settings.GOOGLE_SCOPES))
        if not creds or not creds.valid or missing_scopes:
            if creds and creds.expired and creds.refresh_token and not missing_scopes:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(creds_path, settings.GOOGLE_SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, "w", encoding="utf-8") as handle:
                handle.write(creds.to_json())

        return build("keep", "v1", credentials=creds, cache_discovery=False)

    def list_notes(self, *, filter_expression: str = "-trashed", page_size: int = 100) -> list[dict]:
        notes: list[dict] = []
        page_token = ""
        while True:
            params = {"pageSize": page_size}
            if filter_expression:
                params["filter"] = filter_expression
            if page_token:
                params["pageToken"] = page_token
            response = self._service.notes().list(**params).execute()
            notes.extend(response.get("notes", []))
            page_token = response.get("nextPageToken", "")
            if not page_token:
                return notes

    def find_notes_by_title(self, title: str) -> list[dict]:
        expected = str(title or "").strip()
        if not expected:
            return []
        return [
            note
            for note in self.list_notes()
            if str(note.get("title", "")).strip() == expected
        ]

    def delete_note(self, name: str) -> None:
        self._service.notes().delete(name=name).execute()

    def create_checklist_note(self, title: str, items: list[dict]) -> dict:
        body = {
            "title": str(title or "").strip(),
            "body": {
                "list": {
                    "listItems": [
                        {
                            "text": {"text": str(item.get("text", ""))},
                            "checked": bool(item.get("checked", False)),
                        }
                        for item in items
                    ]
                }
            },
        }
        return self._service.notes().create(body=body).execute()


class GoogleKeepTaskSync:
    def __init__(
        self,
        *,
        client: GoogleKeepClient | None = None,
        note_title: str | None = None,
        max_items: int = 1000,
    ):
        resolved_title = str(note_title or settings.GOOGLE_KEEP_TASKS_NOTE_TITLE).strip()
        if not resolved_title:
            raise ValueError("Google Keep task note title cannot be empty.")
        self._client = client or GoogleKeepClient()
        self._note_title = resolved_title
        self._max_items = max(1, max_items)

    def sync(self, tasks: list[dict]) -> dict:
        started = monotonic()
        serialized = self._serialize_tasks(tasks)
        pending_count = sum(1 for task in tasks if str(task.get("status", "")) == "pending")
        done_count = sum(1 for task in tasks if str(task.get("status", "")) == "done")

        try:
            existing = self._client.find_notes_by_title(self._note_title)
            for note in existing:
                if note.get("name"):
                    self._client.delete_note(str(note["name"]))
            created = self._client.create_checklist_note(self._note_title, serialized)
            record_activity(
                event="keep_task_sync_completed",
                component="keep",
                summary="Synced task list to Google Keep",
                duration_ms=(monotonic() - started) * 1000,
                metadata={
                    "note_title": self._note_title,
                    "task_count": len(tasks),
                    "pending_count": pending_count,
                    "done_count": done_count,
                    "replaced_notes": len(existing),
                },
            )
            record_audit(
                event="keep_task_note_synced",
                component="keep",
                summary="Synced task note to Google Keep",
                metadata={
                    "note_title": self._note_title,
                    "task_count": len(tasks),
                    "pending_count": pending_count,
                    "done_count": done_count,
                },
            )
            return {
                "name": str(created.get("name", "")),
                "title": str(created.get("title", self._note_title)),
                "task_count": len(serialized),
            }
        except Exception as exc:
            record_issue(
                level="ERROR",
                event="keep_task_sync_failed",
                component="keep",
                status="error",
                summary="Failed to sync task list to Google Keep",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"note_title": self._note_title, "error": str(exc)},
            )
            raise

    def _serialize_tasks(self, tasks: list[dict]) -> list[dict]:
        ordered = sorted(tasks, key=self._task_sort_key)
        items = [
            {
                "text": self._render_task_text(task),
                "checked": str(task.get("status", "")).strip().lower() == "done",
            }
            for task in ordered
        ]
        if len(items) <= self._max_items:
            return items

        visible_items = items[: self._max_items - 1]
        hidden_count = len(items) - len(visible_items)
        visible_items.append(
            {
                "text": f"... {hidden_count} more task(s) not shown",
                "checked": False,
            }
        )
        return visible_items

    def _task_sort_key(self, task: dict) -> tuple[int, str, str, str]:
        status = str(task.get("status", "")).strip().lower()
        status_rank = 0 if status == "pending" else 1
        due_date = str(task.get("due_date", "") or "9999-12-31")
        created_at = str(task.get("created_at", "") or "")
        task_id = str(task.get("id", "") or "")
        return status_rank, due_date, created_at, task_id

    def _render_task_text(self, task: dict) -> str:
        description = " ".join(str(task.get("description", "") or "").split()) or "(untitled task)"
        due_date = str(task.get("due_date", "") or "").strip()
        suffix = f" (due {due_date})" if due_date else ""
        max_description_length = max(1, 1000 - len(suffix))
        if len(description) > max_description_length:
            description = description[: max_description_length - 3].rstrip() + "..."
        return f"{description}{suffix}"
