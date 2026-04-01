from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memory.manager import MemoryManager

_BASE_SYSTEM_PROMPT = """\
You are Jarvis, a personal AI assistant for Wess. You run locally on his Mac Mini and \
are accessible via Telegram.

Your responsibilities:
- Answer questions about anything Wess shares with you or that you have stored in memory.
- Manage your memory by deciding what is worth remembering from each conversation.
- Help Wess query his Google Drive document library.
- There is no proactive behaviour — you respond only when spoken to.

Behaviour rules:
- Be concise and direct. Wess prefers short answers unless he asks for detail.
- Never hallucinate facts. If you don't know something, say so.
- Memory updates are your responsibility. After each meaningful exchange, decide whether \
anything should be stored, updated, or forgotten.

Available tools:
- remember(topic, summary, category, source, confidence): store or update a memory.
- recall(query): semantic search over your memory store.
- forget(topic): delete a memory by topic.
- list_memories(category?): list all stored memories, optionally by category.
- search_drive(query): search Google Drive for a file.
"""

_MEMORY_SECTION_HEADER = "\n\n---\nRelevant memories from your knowledge base:\n"
_MEMORY_SECTION_FOOTER = "\n---\n"


def build_system_prompt(memory_manager: "MemoryManager", user_message: str) -> str:
    """Build a system prompt with top-N semantically relevant memories injected."""
    memories = memory_manager.search(user_message, n_results=8)

    if not memories:
        return _BASE_SYSTEM_PROMPT

    memory_lines = []
    for m in memories:
        line = f"[{m.category.value.upper()} | {m.topic}] {m.summary}"
        if m.document_ref:
            line += f" (Drive ID: {m.document_ref})"
        memory_lines.append(line)

    return (
        _BASE_SYSTEM_PROMPT
        + _MEMORY_SECTION_HEADER
        + "\n".join(memory_lines)
        + _MEMORY_SECTION_FOOTER
    )
