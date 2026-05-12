from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from config import settings
from core.opslog import record_activity, record_audit, record_issue
from gmail.action_extractor import EmailActionProposal
from memory.schema import MemoryCategory, MemoryConfidence, MemoryRecord, MemorySource

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS gmail_action_proposals (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    committed_at TEXT,
    dismissed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gmail_action_proposals_status ON gmail_action_proposals(status);
CREATE INDEX IF NOT EXISTS idx_gmail_action_proposals_message_id ON gmail_action_proposals(message_id);
"""


class GmailActionManager:
    def __init__(
        self,
        *,
        memory_manager=None,
        calendar_client=None,
        reminder_manager=None,
        db_path: str | None = None,
    ) -> None:
        self._memory = memory_manager
        self._calendar = calendar_client
        self._reminders = reminder_manager
        path = db_path or settings.JARVIS_DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_CREATE_TABLE)
        self._db.commit()
        self._lock = threading.Lock()

    def store_proposal(self, proposal: EmailActionProposal) -> dict[str, Any]:
        proposal_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        payload = proposal.to_dict()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO gmail_action_proposals
                    (id, message_id, thread_id, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    proposal_id,
                    proposal.message_id,
                    proposal.thread_id,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._db.commit()

        record_audit(
            event="gmail_action_proposal_created",
            component="gmail",
            summary="Stored Gmail action proposal awaiting confirmation",
            metadata={"proposal_id": proposal_id, "message_id": proposal.message_id},
        )
        return self.get_proposal(proposal_id) or {"id": proposal_id, "payload": payload, "status": "pending"}

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM gmail_action_proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return data

    def dismiss_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute(
                """
                UPDATE gmail_action_proposals
                SET status='dismissed', dismissed_at=?, updated_at=?
                WHERE id=? AND status='pending'
                """,
                (now, now, proposal_id),
            )
            self._db.commit()
        proposal = self.get_proposal(proposal_id)
        if proposal:
            record_activity(
                event="gmail_action_proposal_dismissed",
                component="gmail",
                summary="Dismissed Gmail action proposal",
                metadata={"proposal_id": proposal_id},
            )
        return proposal

    def commit_proposal(self, proposal_id: str) -> tuple[dict[str, Any] | None, list[str]]:
        proposal = self.get_proposal(proposal_id)
        if not proposal or proposal.get("status") != "pending":
            return proposal, []

        payload = proposal["payload"]
        results: list[str] = []

        results.extend(self._commit_calendar_events(payload.get("calendar_events") or []))
        results.extend(self._commit_tasks(payload.get("tasks") or []))
        results.extend(self._commit_reminders(payload.get("reminders") or []))
        results.extend(self._commit_memory_updates(payload.get("memory_updates") or []))
        if payload.get("reply_bullets"):
            results.append(f"Kept {len(payload['reply_bullets'])} reply bullet(s) in the proposal for reference.")

        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute(
                """
                UPDATE gmail_action_proposals
                SET status='committed', committed_at=?, updated_at=?
                WHERE id=? AND status='pending'
                """,
                (now, now, proposal_id),
            )
            self._db.commit()

        record_audit(
            event="gmail_action_proposal_committed",
            component="gmail",
            summary="Committed confirmed Gmail action proposal",
            metadata={"proposal_id": proposal_id, "result_count": len(results)},
        )
        return self.get_proposal(proposal_id), results

    def _commit_calendar_events(self, events: list[dict[str, Any]]) -> list[str]:
        if not events:
            return []
        if not self._calendar:
            return [f"Skipped {len(events)} calendar event(s): calendar is not available."]
        results = []
        for event in events:
            try:
                created = self._calendar.create_event(
                    summary=event["summary"],
                    start=event["start"],
                    end=event.get("end") or "",
                    description=event.get("description") or "",
                    location=event.get("location") or "",
                    all_day=bool(event.get("all_day", False)),
                )
                results.append(f"Created calendar event: {created.get('summary') or event['summary']}")
            except Exception as exc:
                logger.exception("Failed to commit Gmail calendar proposal")
                record_issue(
                    level="ERROR",
                    event="gmail_action_calendar_commit_failed",
                    component="gmail",
                    status="error",
                    summary="Failed to create confirmed Gmail calendar event",
                    metadata={"error": str(exc)[:300]},
                )
                results.append(f"Calendar event failed: {event.get('summary', '(untitled)')}")
        return results

    def _commit_tasks(self, tasks: list[dict[str, Any]]) -> list[str]:
        if not tasks:
            return []
        if not self._memory:
            return [f"Skipped {len(tasks)} task(s): memory manager is not available."]
        results = []
        for task in tasks:
            created = self._memory.create_task(
                task["description"],
                task.get("due_date") or None,
                source="gmail",
                surfaced=True,
            )
            results.append(f"Created task: {created['description']}")
        return results

    def _commit_reminders(self, reminders: list[dict[str, Any]]) -> list[str]:
        if not reminders:
            return []
        if not self._reminders:
            return [f"Skipped {len(reminders)} reminder(s): reminder manager is not available."]
        results = []
        for reminder in reminders:
            try:
                created = self._reminders.schedule_message(
                    reminder["message"],
                    reminder["when"],
                )
                results.append(f"Scheduled reminder: {created['message']}")
            except Exception as exc:
                logger.exception("Failed to commit Gmail reminder proposal")
                record_issue(
                    level="ERROR",
                    event="gmail_action_reminder_commit_failed",
                    component="gmail",
                    status="error",
                    summary="Failed to schedule confirmed Gmail reminder",
                    metadata={"error": str(exc)[:300]},
                )
                results.append(f"Reminder failed: {reminder.get('message', '(untitled)')}")
        return results

    def _commit_memory_updates(self, updates: list[dict[str, Any]]) -> list[str]:
        if not updates:
            return []
        if not self._memory:
            return [f"Skipped {len(updates)} memory update(s): memory manager is not available."]
        results = []
        for update in updates:
            record = MemoryRecord(
                topic=update["topic"],
                summary=update["summary"],
                category=MemoryCategory(update.get("category") or "fact"),
                source=MemorySource.EMAIL,
                confidence=MemoryConfidence(update.get("confidence") or "medium"),
            )
            self._memory.upsert(record)
            results.append(f"Updated memory: {record.topic}")
        return results


def format_gmail_action_card(proposal: dict[str, Any]) -> str:
    payload = proposal["payload"]
    lines = [
        "[Gmail] Proposed actions",
        f"From: {_trim(payload.get('sender'), 100)}",
        f"Subject: {_trim(payload.get('subject'), 120)}",
    ]
    if payload.get("summary"):
        lines.append(f"Summary: {_trim(payload['summary'], 220)}")
    if payload.get("rationale"):
        lines.append(f"Why: {_trim(payload['rationale'], 220)}")

    _append_items(lines, "Calendar", payload.get("calendar_events"), lambda item: item.get("summary", ""))
    _append_items(lines, "Tasks", payload.get("tasks"), lambda item: item.get("description", ""))
    _append_items(lines, "Reminders", payload.get("reminders"), lambda item: f"{item.get('message', '')} ({item.get('when', '')})")
    _append_items(lines, "Memory", payload.get("memory_updates"), lambda item: item.get("topic", ""))
    _append_items(lines, "Reply bullets", payload.get("reply_bullets"), lambda item: str(item))

    lines.append("")
    lines.append("Confirm to create the proposed items. Dismiss leaves no side effects.")
    return "\n".join(line for line in lines if line is not None)


def build_gmail_action_reply_markup(proposal_id: str):
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:
        return None

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data=f"gmailaction:confirm:{proposal_id}"),
                InlineKeyboardButton("Dismiss", callback_data=f"gmailaction:dismiss:{proposal_id}"),
            ]
        ]
    )


def format_commit_result(results: list[str]) -> str:
    if not results:
        return "No actions were committed."
    lines = ["Gmail actions confirmed:"]
    lines.extend(f"- {result}" for result in results)
    return "\n".join(lines)


def _append_items(lines: list[str], label: str, items: Any, formatter) -> None:
    if not items:
        return
    lines.append(f"{label}:")
    for item in list(items)[:5]:
        rendered = formatter(item)
        if rendered:
            lines.append(f"- {_trim(rendered, 180)}")
    if len(items) > 5:
        lines.append(f"- ... and {len(items) - 5} more")


def _trim(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
