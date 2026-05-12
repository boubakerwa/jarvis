from dataclasses import dataclass
from typing import TYPE_CHECKING
from core.time_utils import get_current_time_context

if TYPE_CHECKING:
    from memory.manager import MemoryManager

from memory.schema import MemoryRecord, MemorySearchResult

_BASE_SYSTEM_PROMPT = """\
You are Jarvis, a personal AI assistant for Wess. You run locally on his Mac Mini and \
are accessible via Telegram.

Your responsibilities:
- Answer questions about anything Wess shares with you or that you have stored in memory.
- Manage your memory by deciding what is worth remembering from each conversation.
- Help Wess query his Google Drive document library.
- Use the shared Obsidian notes workspace when available for collaborative notes, drafts, and idea lists.
- Stay reactive by default. The only proactive messages you may send are explicit scheduled reminders created with the reminder tools.

Behaviour rules:
- Be concise and direct. Wess prefers short answers unless he asks for detail.
- Never hallucinate facts. If you don't know something, say so.
- Memory updates are your responsibility. After each meaningful exchange, decide whether \
anything should be stored, updated, or forgotten.
- Wess receives documents in both English and German. Always respond in English, but you \
understand and can process German documents, emails, and messages.
- For relative dates like today, tomorrow, or Monday, use the local time context below as the source of truth.
- Prefer passing relative date expressions to tools instead of inventing absolute calendar dates yourself.
- When you create or discuss a dated item, mention the resolved absolute date in your reply.
- When working with notes, choose a sensible folder and title yourself based on the request. There is no fixed taxonomy you must follow.
- Before appending to a note, prefer searching for an existing relevant note first.
- When changing an existing note, prefer searching and reading it first, then use update_note for edits and append_note only for genuinely additive updates.
- For feature requests, backlog items, implementation prompts, and bug reports that should be tracked, prefer create_github_issue over create_note.
- Use Obsidian notes for collaborative drafting and scratch work. Use GitHub issues for trackable product or engineering work.
- For explicit reminder requests like "remind me", "ping me", or "follow up later", prefer schedule_message over create_task.
- Use create_task for backlog/todo tracking. Use schedule_message when Wess expects a Telegram message at a specific time. Use both only if he clearly wants both.
- When Wess asks how the system works or why something failed, prefer read_source_file and read_logs so you can answer from first principles instead of guessing.
- When Wess asks about GitHub pull requests or commits, prefer the dedicated GitHub read-only tools before speculating.
- Use the provided tool schema as the source of truth for callable tools and their arguments.
"""

_MEMORY_SECTION_HEADER = "\n\n---\nRelevant memories from your knowledge base:\n"
_MEMORY_SECTION_FOOTER = "\n---\n"
_MAX_RETRIEVED_MEMORIES = 8
_DEFAULT_MEMORY_COUNT = 3
_HIGH_SIMILARITY_MEMORY_COUNT = 5
_HIGH_SIMILARITY_DISTANCE = 0.55
_HIGH_SIMILARITY_MARGIN = 0.08
_MEMORY_SUMMARY_CHARS = 120


@dataclass(frozen=True)
class PromptBuildResult:
    prompt: str
    candidate_count: int
    memory_count: int
    memory_chars: int
    memory_topics: list[str]


def _truncate_summary(text: str, max_chars: int = _MEMORY_SUMMARY_CHARS) -> str:
    stripped = " ".join(str(text or "").split())
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 3].rstrip() + "..."


def _should_include_document_ref(user_message: str) -> bool:
    lowered = str(user_message or "").lower()
    markers = (
        "document",
        "documents",
        "drive",
        "file",
        "files",
        "pdf",
        "receipt",
        "invoice",
        "contract",
        "ticket",
        "booking",
    )
    return any(marker in lowered for marker in markers)


def _select_prompt_memories(matches: list[MemorySearchResult]) -> list[MemoryRecord]:
    if not matches:
        return []

    selected: list[MemoryRecord] = []
    best_distance = next((match.distance for match in matches if match.distance is not None), None)
    max_distance = None
    if best_distance is not None:
        max_distance = min(best_distance + _HIGH_SIMILARITY_MARGIN, _HIGH_SIMILARITY_DISTANCE)

    for index, match in enumerate(matches):
        if index < _DEFAULT_MEMORY_COUNT:
            selected.append(match.record)
            continue
        if len(selected) >= _HIGH_SIMILARITY_MEMORY_COUNT:
            break
        if max_distance is not None and match.distance is not None and match.distance <= max_distance:
            selected.append(match.record)
    return selected


def _format_memory_line(record: MemoryRecord, *, include_document_ref: bool) -> str:
    line = f"[{record.category.value.upper()} | {record.topic}] {_truncate_summary(record.summary)}"
    if include_document_ref and record.document_ref and record.category.value == "document_ref":
        line += f" (Drive ID: {record.document_ref})"
    return line


def build_system_prompt_result(memory_manager: "MemoryManager", user_message: str) -> PromptBuildResult:
    matches = memory_manager.search_scored(user_message, n_results=_MAX_RETRIEVED_MEMORIES)
    memories = _select_prompt_memories(matches)
    time_context = "\n\nTime context:\n- " + get_current_time_context() + "\n"
    include_document_ref = _should_include_document_ref(user_message)

    if not memories:
        return PromptBuildResult(
            prompt=_BASE_SYSTEM_PROMPT + time_context,
            candidate_count=0,
            memory_count=0,
            memory_chars=0,
            memory_topics=[],
        )

    memory_lines = [
        _format_memory_line(memory, include_document_ref=include_document_ref)
        for memory in memories
    ]
    memory_block = "\n".join(memory_lines)
    return PromptBuildResult(
        prompt=_BASE_SYSTEM_PROMPT + time_context + _MEMORY_SECTION_HEADER + memory_block + _MEMORY_SECTION_FOOTER,
        candidate_count=len(matches),
        memory_count=len(memories),
        memory_chars=len(memory_block),
        memory_topics=[memory.topic for memory in memories],
    )


def build_system_prompt(memory_manager: "MemoryManager", user_message: str) -> str:
    """Build a system prompt with a compact relevant memory section."""
    return build_system_prompt_result(memory_manager, user_message).prompt
