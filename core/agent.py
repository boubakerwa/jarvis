import json
import logging
from typing import Optional

import anthropic

from config import settings
from core.prompts import build_system_prompt
from memory.manager import MemoryManager
from memory.schema import MemoryCategory, MemoryConfidence, MemoryRecord, MemorySource

logger = logging.getLogger(__name__)

# Tool definitions passed to Claude
TOOLS: list[dict] = [
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
                    "enum": ["preference", "fact", "decision", "document_ref", "project", "household", "finance", "health"],
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
                    "enum": ["preference", "fact", "decision", "document_ref", "project", "household", "finance", "health"],
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
]


class JarvisAgent:
    def __init__(self, memory_manager: MemoryManager, drive_client=None):
        self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._memory = memory_manager
        self._drive = drive_client
        self._history: list[dict] = []

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def chat(self, user_message: str) -> str:
        """Process a user message and return Jarvis's response."""
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
            response = self._client.messages.create(
                model=settings.CLAUDE_MODEL,
                max_tokens=settings.MAX_TOKENS,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            # Collect all tool uses and text from this response
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

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
            if name == "remember":
                return self._tool_remember(inputs)
            elif name == "recall":
                return self._tool_recall(inputs)
            elif name == "forget":
                return self._tool_forget(inputs)
            elif name == "list_memories":
                return self._tool_list_memories(inputs)
            elif name == "search_drive":
                return self._tool_search_drive(inputs)
            else:
                return f"Unknown tool: {name}"
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return f"Error executing {name}: {e}"

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # History management
    # ------------------------------------------------------------------

    def _trim_history(self) -> None:
        """Keep at most MAX_CONVERSATION_TURNS * 2 messages (user+assistant pairs)."""
        max_messages = settings.MAX_CONVERSATION_TURNS * 2
        if len(self._history) > max_messages:
            self._history = self._history[-max_messages:]
