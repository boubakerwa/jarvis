import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Optional

from config import settings
from core.llmops import record_llm_call
from core.llm_client import call_with_free_model_retry, create_llm_client, get_model_name
from core.prompts import build_system_prompt
from core.time_utils import (
    contains_explicit_date,
    day_bounds_for_calendar,
    extract_relative_date_expression,
    get_local_now,
    resolve_date_expression,
    resolve_event_time,
)
from memory.manager import MemoryManager
from memory.schema import MemoryCategory, MemoryConfidence, MemoryRecord, MemorySource

logger = logging.getLogger(__name__)

# Tool definitions passed to the model
TOOLS: list[dict] = [
    {
        "name": "schedule_message",
        "description": (
            "Schedule a proactive Telegram message for Wess. "
            "Use this for explicit reminder requests like 'remind me', 'ping me later', or 'follow up in 2 hours'. "
            "You may also set recurrence for repeating reminders."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The exact message Jarvis should send to Wess later via Telegram.",
                },
                "when": {
                    "type": "string",
                    "description": "When to send it. Accepts ISO datetimes or natural language like 'today at 3pm' or 'in 2 hours'.",
                },
                "recurrence": {
                    "type": "string",
                    "description": "Optional repeat rule such as 'daily', 'weekly', 'every 2 hours', or 'weekdays'.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Optional linked task ID. Useful for repeated reminders tied to a task.",
                },
                "until_task_done": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, stop sending this reminder after the linked task is marked done.",
                },
            },
            "required": ["message", "when"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List scheduled Telegram reminders so you can review what is queued.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["scheduled", "cancelled", "completed", "all"],
                    "default": "scheduled",
                },
            },
        },
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a scheduled reminder by ID. Accepts the full ID or the short 8-character prefix from list_reminders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "description": "Reminder ID from list_reminders."},
            },
            "required": ["reminder_id"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Store or update a memory. If a memory with this topic already exists, "
            "it will be replaced. Use this to persist facts, preferences, decisions, "
            "document references, and other important information about Wess."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Unique key for this memory, e.g. 'health insurer', 'travel preferences'.",
                },
                "summary": {
                    "type": "string",
                    "description": "1-2 sentence description. What you should know.",
                },
                "category": {
                    "type": "string",
                    "enum": ["preference", "fact", "decision", "document_ref", "project", "household", "finance", "health", "task"],
                },
                "source": {
                    "type": "string",
                    "enum": ["telegram", "email", "document", "manual"],
                    "default": "telegram",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "default": "high",
                },
            },
            "required": ["topic", "summary", "category"],
        },
    },
    {
        "name": "recall",
        "description": "Semantic search over your memory store. Returns the most relevant memories for the query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "n_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "forget",
        "description": "Delete a memory by topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "list_memories",
        "description": "List all stored memories, optionally filtered by category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["preference", "fact", "decision", "document_ref", "project", "household", "finance", "health", "task"],
                    "description": "Optional category filter.",
                },
            },
        },
    },
    {
        "name": "search_drive",
        "description": "Search Google Drive for files by name or content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_drive_file",
        "description": (
            "Download and read the contents of a file from Google Drive. "
            "Use file IDs returned by search_drive. Enables document Q&A."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Google Drive file ID."},
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "create_note",
        "description": (
            "Create a note in the shared Obsidian notes workspace. "
            "Choose the folder and title that best fit the request."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Human-readable note title."},
                "body": {"type": "string", "description": "Markdown note body."},
                "folder": {"type": "string", "description": "Optional folder under the shared Marvis notes root."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for note frontmatter.",
                },
                "note_type": {"type": "string", "description": "Optional note type to store in frontmatter."},
                "unique": {"type": "boolean", "default": False},
            },
            "required": ["title"],
        },
    },
    {
        "name": "append_note",
        "description": "Append Markdown content to an existing note in the shared Obsidian notes workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Existing note path."},
                "content": {"type": "string", "description": "Markdown content to append."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "update_note",
        "description": (
            "Modify an existing note in the shared Obsidian notes workspace. "
            "Either replace the full Markdown content or replace exact text inside the note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Existing note path."},
                "content": {
                    "type": "string",
                    "description": (
                        "Full Markdown content that should replace the current note. "
                        "When omitted, use find_text and replace_with instead."
                    ),
                },
                "find_text": {
                    "type": "string",
                    "description": "Exact existing text to replace inside the note.",
                },
                "replace_with": {
                    "type": "string",
                    "description": "Replacement text for find_text. Can be empty to remove text.",
                },
                "replace_all": {"type": "boolean", "default": False},
                "preserve_frontmatter": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "When replacing the full note content, keep existing frontmatter unless the new content already includes one."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_notes",
        "description": "Search the shared Obsidian notes workspace by filename and note content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for in notes."},
                "folder": {"type": "string", "description": "Optional folder under the Marvis notes root."},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_note",
        "description": "Read a note from the shared Obsidian workspace using its path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Note path returned by search_notes or a save tool."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_recent_notes",
        "description": "List recent notes from the shared Obsidian workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Optional folder under the Marvis notes root."},
                "limit": {"type": "integer", "default": 8},
            },
        },
    },
    {
        "name": "check_calendar",
        "description": (
            "Check Google Calendar events. "
            "Prefer date_expression for relative dates like 'today', 'tomorrow', or 'monday'. "
            "Use start_date/end_date only for explicit YYYY-MM-DD dates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_expression": {
                    "type": "string",
                    "description": "Preferred for relative dates such as 'today', 'tomorrow', 'monday', or 'next friday'.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD). Defaults to today.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD). Defaults to end of start_date.",
                },
                "max_results": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "create_event",
        "description": (
            "Create a new Google Calendar event. "
            "Prefer when for relative or natural-language dates/times; use start/end only for explicit ISO datetimes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title."},
                "when": {
                    "type": "string",
                    "description": "Preferred natural-language date/time, e.g. 'monday', 'tomorrow at 3pm', '2026-04-06'.",
                },
                "start": {
                    "type": "string",
                    "description": "Start datetime (ISO 8601 with timezone, e.g. 2026-04-02T14:00:00+02:00).",
                },
                "end": {
                    "type": "string",
                    "description": "End datetime (ISO 8601). Defaults to 1 hour after start.",
                },
                "description": {"type": "string", "description": "Optional event description."},
                "location": {"type": "string", "description": "Optional location."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "create_task",
        "description": (
            "Create a task or todo item in Jarvis's task list. "
            "This does not send Telegram automatically. "
            "ONLY use when Wess explicitly asks — e.g. 'add to my todo' or 'create a task'. "
            "Never create tasks proactively."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What needs to be done."},
                "due_date_expression": {
                    "type": "string",
                    "description": "Preferred for relative dates like 'monday', 'tomorrow', or 'next friday'.",
                },
                "due_date": {"type": "string", "description": "Optional due date (YYYY-MM-DD)."},
            },
            "required": ["description"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List tasks. Use when Wess asks about his tasks, todos, or reminders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "done", "all"],
                    "default": "pending",
                },
            },
        },
    },
    {
        "name": "complete_task",
        "description": "Mark a task as done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID from list_tasks results."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "financial_summary",
        "description": "Query financial records (invoices, receipts, subscriptions, etc.) that have been filed to Drive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD). Defaults to 30 days ago.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD). Defaults to today.",
                },
                "vendor": {"type": "string", "description": "Filter by vendor name (partial match)."},
                "category": {
                    "type": "string",
                    "description": "Filter by category: invoice, receipt, subscription, insurance, tax, bank, other.",
                },
            },
        },
    },
]


class JarvisAgent:
    def __init__(self, memory_manager: MemoryManager, drive_client=None, calendar_client=None, notes_manager=None, reminder_manager=None):
        self._client = create_llm_client()
        self._memory = memory_manager
        self._drive = drive_client
        self._calendar = calendar_client
        self._notes = notes_manager
        self._reminders = reminder_manager
        self._history: list[dict] = []
        self._current_user_message = ""

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def chat(self, user_message: str) -> str:
        """Process a user message and return Jarvis's response."""
        self._current_user_message = user_message
        self._history.append({"role": "user", "content": user_message})
        self._trim_history()

        system_prompt = build_system_prompt(self._memory, user_message)
        response_text = self._run_loop(system_prompt)

        self._history.append({"role": "assistant", "content": response_text})
        return response_text

    def reset_history(self) -> None:
        self._history.clear()

    # ------------------------------------------------------------------
    # Private — agent loop
    # ------------------------------------------------------------------

    def _run_loop(self, system_prompt: str) -> str:
        messages = list(self._history)

        while True:
            model_name = get_model_name()
            started_at = datetime.now(timezone.utc).isoformat()
            started_clock = monotonic()
            try:
                response = call_with_free_model_retry(
                    lambda: self._client.messages.create(
                        model=model_name,
                        max_tokens=settings.MAX_TOKENS,
                        system=system_prompt,
                        tools=TOOLS,
                        messages=messages,
                    ),
                    model_name,
                )
            except Exception as exc:
                record_llm_call(
                    task="chat",
                    model=model_name,
                    status="api_error",
                    started_at=started_at,
                    latency_ms=(monotonic() - started_clock) * 1000,
                    error=str(exc),
                    metadata={"channel": "chat", "history_messages": len(messages)},
                )
                raise

            # Collect all tool uses and text from this response
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]
            record_llm_call(
                task="chat",
                model=model_name,
                status="ok",
                started_at=started_at,
                latency_ms=(monotonic() - started_clock) * 1000,
                response=response,
                metadata={
                    "channel": "chat",
                    "history_messages": len(messages),
                    "tool_use_count": len(tool_uses),
                    "text_block_count": len(text_blocks),
                },
            )

            if response.stop_reason == "end_turn" or not tool_uses:
                # Done — return combined text
                return "\n".join(b.text for b in text_blocks).strip()

            # Append assistant message with all content blocks
            messages.append({"role": "assistant", "content": response.content})

            # Execute all tool calls and collect results
            tool_results = []
            for tool_use in tool_uses:
                result = self._execute_tool(tool_use.name, tool_use.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

    def _execute_tool(self, name: str, inputs: dict) -> str:
        try:
            if name == "schedule_message":
                return self._tool_schedule_message(inputs)
            elif name == "list_reminders":
                return self._tool_list_reminders(inputs)
            elif name == "cancel_reminder":
                return self._tool_cancel_reminder(inputs)
            elif name == "remember":
                return self._tool_remember(inputs)
            elif name == "recall":
                return self._tool_recall(inputs)
            elif name == "forget":
                return self._tool_forget(inputs)
            elif name == "list_memories":
                return self._tool_list_memories(inputs)
            elif name == "search_drive":
                return self._tool_search_drive(inputs)
            elif name == "read_drive_file":
                return self._tool_read_drive_file(inputs)
            elif name == "create_note":
                return self._tool_create_note(inputs)
            elif name == "append_note":
                return self._tool_append_note(inputs)
            elif name == "update_note":
                return self._tool_update_note(inputs)
            elif name == "search_notes":
                return self._tool_search_notes(inputs)
            elif name == "read_note":
                return self._tool_read_note(inputs)
            elif name == "list_recent_notes":
                return self._tool_list_recent_notes(inputs)
            elif name == "check_calendar":
                return self._tool_check_calendar(inputs)
            elif name == "create_event":
                return self._tool_create_event(inputs)
            elif name == "create_task":
                return self._tool_create_task(inputs)
            elif name == "list_tasks":
                return self._tool_list_tasks(inputs)
            elif name == "complete_task":
                return self._tool_complete_task(inputs)
            elif name == "financial_summary":
                return self._tool_financial_summary(inputs)
            else:
                return f"Unknown tool: {name}"
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return f"Error executing {name}: {e}"

    def _relative_date_hint_from_user(self) -> Optional[str]:
        text = self._current_user_message or ""
        if not text or contains_explicit_date(text):
            return None
        return extract_relative_date_expression(text)

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_schedule_message(self, inputs: dict) -> str:
        if not self._reminders:
            return "Reminder manager not initialised."

        try:
            reminder = self._reminders.schedule_message(
                inputs["message"],
                inputs["when"],
                recurrence=inputs.get("recurrence"),
                task_id=inputs.get("task_id"),
                until_task_done=inputs.get("until_task_done", False),
                now=get_local_now(),
            )
        except ValueError as e:
            return f"Could not schedule reminder: {e}"

        short_id = reminder["id"][:8]
        scheduled_for = self._reminders.describe_reminder(reminder)
        return f"Reminder scheduled: {scheduled_for} (ID: {short_id})"

    def _tool_list_reminders(self, inputs: dict) -> str:
        if not self._reminders:
            return "Reminder manager not initialised."
        status = inputs.get("status", "scheduled")
        reminders = self._reminders.list_reminders(status)
        if not reminders:
            return f"No {status} reminders." if status != "all" else "No reminders found."
        return "\n".join(f"- {self._reminders.describe_reminder(reminder)}" for reminder in reminders)

    def _tool_cancel_reminder(self, inputs: dict) -> str:
        if not self._reminders:
            return "Reminder manager not initialised."
        try:
            reminder = self._reminders.cancel_reminder(inputs["reminder_id"], now=get_local_now())
        except ValueError as e:
            return f"Could not cancel reminder: {e}"
        if reminder is None:
            return f"No scheduled reminder found with ID starting with '{inputs['reminder_id']}'."
        return f"Reminder cancelled: {self._reminders.describe_reminder(reminder)}"

    def _tool_remember(self, inputs: dict) -> str:
        record = MemoryRecord(
            topic=inputs["topic"],
            summary=inputs["summary"],
            category=MemoryCategory(inputs["category"]),
            source=MemorySource(inputs.get("source", "telegram")),
            confidence=MemoryConfidence(inputs.get("confidence", "high")),
        )
        self._memory.upsert(record)
        return f"Memory stored: [{record.category.value}] {record.topic}"

    def _tool_recall(self, inputs: dict) -> str:
        records = self._memory.search(inputs["query"], n_results=inputs.get("n_results", 5))
        if not records:
            return "No relevant memories found."
        lines = [f"[{r.category.value}] {r.topic}: {r.summary}" for r in records]
        return "\n".join(lines)

    def _tool_forget(self, inputs: dict) -> str:
        deleted = self._memory.forget(inputs["topic"])
        return f"Memory '{inputs['topic']}' deleted." if deleted else f"No memory found for topic '{inputs['topic']}'."

    def _tool_list_memories(self, inputs: dict) -> str:
        category_str = inputs.get("category")
        category = MemoryCategory(category_str) if category_str else None
        records = self._memory.list_all(category=category)
        if not records:
            return "No memories stored."
        # Group by category
        grouped: dict[str, list[str]] = {}
        for r in records:
            grouped.setdefault(r.category.value, []).append(f"  - [{r.topic}] {r.summary}")
        lines = []
        for cat, items in grouped.items():
            lines.append(f"\n{cat.upper()}")
            lines.extend(items)
        return "\n".join(lines).strip()

    def _tool_search_drive(self, inputs: dict) -> str:
        if not self._drive:
            return "Drive client not initialised."
        query = inputs["query"]
        results = self._drive.search(query)
        if not results:
            return "No files found."
        lines = [f"- {r['name']} (ID: {r['id']}, path: {r.get('path', 'unknown')})" for r in results]
        return "\n".join(lines)

    def _tool_read_drive_file(self, inputs: dict) -> str:
        if not self._drive:
            return "Drive client not initialised."
        file_id = inputs["file_id"]
        try:
            data, filename, mime_type = self._drive.download_file(file_id)
        except Exception as e:
            return f"Failed to download file: {e}"

        from utils.text_extraction import describe_image, extract_text
        if mime_type.startswith("image/"):
            text = describe_image(data, mime_type)
        else:
            text = extract_text(data, mime_type, filename)

        if not text:
            return f"Could not extract text from '{filename}' (type: {mime_type})."

        # Truncate to avoid blowing context window
        max_chars = 8000
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[...truncated — {len(text) - max_chars} more chars]"
        return f"Contents of '{filename}':\n\n{text}"

    def _tool_create_note(self, inputs: dict) -> str:
        if not self._notes:
            return "Notes workspace not initialised."
        result = self._notes.create_note(
            title=inputs["title"],
            body=inputs.get("body", ""),
            folder=inputs.get("folder", ""),
            tags=inputs.get("tags"),
            note_type=inputs.get("note_type", ""),
            unique=inputs.get("unique", False),
        )
        return f"Note created in {result['path']}."

    def _tool_append_note(self, inputs: dict) -> str:
        if not self._notes:
            return "Notes workspace not initialised."
        result = self._notes.append_note(inputs["path"], inputs["content"])
        return f"Note updated: {result['path']}."

    def _tool_update_note(self, inputs: dict) -> str:
        if not self._notes:
            return "Notes workspace not initialised."
        result = self._notes.update_note(
            inputs["path"],
            content=inputs.get("content"),
            find_text=inputs.get("find_text"),
            replace_with=inputs.get("replace_with"),
            replace_all=inputs.get("replace_all", False),
            preserve_frontmatter=inputs.get("preserve_frontmatter", True),
        )
        if result["mode"] == "replace_text":
            return (
                f"Note updated: {result['path']} "
                f"({result['replacement_count']} replacement{'s' if result['replacement_count'] != 1 else ''})."
            )
        return f"Note updated: {result['path']}."

    def _tool_search_notes(self, inputs: dict) -> str:
        if not self._notes:
            return "Notes workspace not initialised."
        matches = self._notes.search_notes(
            inputs["query"],
            folder=inputs.get("folder"),
            limit=inputs.get("limit", 5),
        )
        if not matches:
            return "No notes found."
        lines = []
        for match in matches:
            lines.append(
                f"- {match['path']} ({match['modified_at']})\n  {match['snippet']}"
            )
        return "\n".join(lines)

    def _tool_read_note(self, inputs: dict) -> str:
        if not self._notes:
            return "Notes workspace not initialised."
        note = self._notes.read_note(inputs["path"])
        return f"Contents of {note['path']}:\n\n{note['content']}"

    def _tool_list_recent_notes(self, inputs: dict) -> str:
        if not self._notes:
            return "Notes workspace not initialised."
        notes = self._notes.list_recent_notes(
            folder=inputs.get("folder"),
            limit=max(1, inputs.get("limit", 8)),
        )
        if not notes:
            return "No notes found."
        lines = []
        for note in notes:
            lines.append(f"- {note['path']} ({note['modified_at']})\n  {note['snippet']}")
        return "\n".join(lines)

    def _tool_financial_summary(self, inputs: dict) -> str:
        from datetime import date, timedelta
        today = date.today()
        default_start = (today - timedelta(days=30)).isoformat()

        start_date = inputs.get("start_date", default_start)
        end_date = inputs.get("end_date", today.isoformat())
        vendor = inputs.get("vendor")
        category = inputs.get("category")

        if vendor or category:
            records = self._memory.query_financials(start_date, end_date, vendor, category)
            if not records:
                return "No financial records found matching your query."
            lines = []
            for r in records:
                lines.append(
                    f"- {r.get('date', '?')} | {r.get('vendor', '?')} | "
                    f"{r.get('amount', 0):.2f} {r.get('currency', 'EUR')} | {r.get('category', '?')}"
                )
            return "\n".join(lines)

        summary = self._memory.financial_summary(start_date, end_date)
        if summary["record_count"] == 0:
            return f"No financial records between {start_date} and {end_date}."

        lines = [f"Financial summary ({start_date} → {end_date}):"]
        lines.append(f"Total: {summary['total']:.2f} EUR ({summary['record_count']} records)")
        if summary["by_category"]:
            lines.append("\nBy category:")
            for cat, amt in sorted(summary["by_category"].items(), key=lambda x: -x[1]):
                lines.append(f"  {cat}: {amt:.2f}")
        if summary["by_vendor"]:
            lines.append("\nTop vendors:")
            top_vendors = sorted(summary["by_vendor"].items(), key=lambda x: -x[1])[:5]
            for vendor_name, amt in top_vendors:
                lines.append(f"  {vendor_name}: {amt:.2f}")
        return "\n".join(lines)

    def _tool_create_task(self, inputs: dict) -> str:
        description = inputs["description"]
        due_expression = inputs.get("due_date_expression") or self._relative_date_hint_from_user()
        due_date = inputs.get("due_date")
        if due_expression:
            try:
                due_date = resolve_date_expression(due_expression, now=get_local_now()).isoformat()
            except ValueError as e:
                return f"Could not resolve task due date '{due_expression}': {e}"
        task = self._memory.create_task(description, due_date)
        short_id = task["id"][:8]
        due_str = f", due {task['due_date']}" if task.get("due_date") else ""
        return f"Task created (ID: {short_id}{due_str}): {description}"

    def _tool_list_tasks(self, inputs: dict) -> str:
        status = inputs.get("status", "pending")
        tasks = self._memory.list_tasks(status)
        if not tasks:
            return f"No {status} tasks." if status != "all" else "No tasks found."
        lines = []
        for t in tasks:
            short_id = t["id"][:8]
            due_str = f" [due: {t['due_date']}]" if t.get("due_date") else ""
            status_str = f" [{t['status']}]" if status == "all" else ""
            lines.append(f"- [{short_id}]{due_str}{status_str} {t['description']}")
        return "\n".join(lines)

    def _tool_complete_task(self, inputs: dict) -> str:
        task_id = inputs["task_id"]
        # Support short IDs: look up the full ID
        tasks = self._memory.list_tasks("pending")
        full_id = None
        for t in tasks:
            if t["id"].startswith(task_id) or t["id"] == task_id:
                full_id = t["id"]
                break
        if not full_id:
            return f"No pending task found with ID starting with '{task_id}'."
        done = self._memory.complete_task(full_id)
        return "Task marked as done." if done else "Could not complete task."

    def _tool_check_calendar(self, inputs: dict) -> str:
        if not self._calendar:
            return "Calendar client not initialised."

        date_expression = inputs.get("date_expression") or self._relative_date_hint_from_user()
        if date_expression:
            try:
                start_date = resolve_date_expression(date_expression, now=get_local_now())
                end_date = start_date
            except ValueError as e:
                return f"Could not resolve calendar date '{date_expression}': {e}"
        else:
            today = get_local_now().date()
            start_str = inputs.get("start_date", str(today))
            end_str = inputs.get("end_date", start_str)

            try:
                from datetime import date
                start_date = date.fromisoformat(start_str)
                end_date = date.fromisoformat(end_str)
            except ValueError as e:
                return f"Invalid date format: {e}"

        max_results = inputs.get("max_results", 10)
        time_min, time_max = day_bounds_for_calendar(start_date, now=get_local_now())
        if end_date != start_date:
            time_min, _ = day_bounds_for_calendar(start_date, now=get_local_now())
            _, time_max = day_bounds_for_calendar(end_date, now=get_local_now())

        try:
            events = self._calendar.get_events(time_min, time_max, max_results=max_results)
        except Exception as e:
            return f"Failed to fetch calendar events: {e}"

        if not events:
            return f"No events found between {start_date.isoformat()} and {end_date.isoformat()}."

        lines = []
        for e in events:
            line = f"- {e['start']} — {e['summary']}"
            if e.get("location"):
                line += f" @ {e['location']}"
            if e.get("description"):
                line += f"\n  {e['description'][:100]}"
            lines.append(line)
        return "\n".join(lines)

    def _tool_create_event(self, inputs: dict) -> str:
        if not self._calendar:
            return "Calendar client not initialised."
        title = inputs["title"]
        when_expression = inputs.get("when") or self._relative_date_hint_from_user()
        start = inputs.get("start", "")
        end = inputs.get("end", "")
        description = inputs.get("description", "")
        location = inputs.get("location", "")
        all_day = False

        if when_expression:
            try:
                resolved = resolve_event_time(when_expression, now=get_local_now())
            except ValueError as e:
                return f"Could not resolve event time '{when_expression}': {e}"
            start = resolved.start
            end = resolved.end
            all_day = resolved.all_day
        elif not start:
            return "Missing event start time."

        try:
            event = self._calendar.create_event(title, start, end, description, location, all_day=all_day)
        except Exception as e:
            return f"Failed to create event: {e}"

        return (
            f"Event created: '{event['summary']}' "
            f"from {event.get('start', start)} to {event.get('end', end)} "
            f"(ID: {event['id']})"
        )

    # ------------------------------------------------------------------
    # History management
    # ------------------------------------------------------------------

    def _trim_history(self) -> None:
        """Keep at most MAX_CONVERSATION_TURNS * 2 messages (user+assistant pairs)."""
        max_messages = settings.MAX_CONVERSATION_TURNS * 2
        if len(self._history) > max_messages:
            self._history = self._history[-max_messages:]
