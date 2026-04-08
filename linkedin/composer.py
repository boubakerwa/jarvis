"""
LinkedIn draft composer.

Ported from x-ticker-investment/src/linkedinComposer.js.

Design:
- No template fallback. LLM is called directly; if it fails an exception is raised
  so the caller (processor) can mark the draft as failed.
- No personal inferences are stored in the memory system — ever.
- Drafts live exclusively in Google Drive (Jarvis/PR/LinkedIn Composer/).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from config import settings
from core.opslog import record_activity, record_issue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------

DEFAULT_VOICE = "professional"

VOICE_GUIDES: dict[str, str] = {
    "professional": "Crisp, credible, and useful for a broad professional audience.",
    "operator": "Practical, informed, and slightly opinionated without sounding hypey.",
    "founder": "Forward-looking and energetic, but still grounded in real implications.",
}

WRITER_CONTEXT = [
    "Writer identity: the user is an independent LinkedIn creator building a personal brand.",
    "Do not write as the source author or imply affiliation with the source account.",
    "Frame the source as something the user saw or read, then add the user's own take.",
    "Prefer framing like 'I came across this post' or 'What stood out to me' over source-centered phrasing.",
    "Keep the tone smart, practical, and personal without sounding self-important.",
]

REWRITE_PRESETS = [
    {
        "id": "builder-voice",
        "label": "Builder Voice",
        "instruction": "Rewrite this in my voice as an independent builder sharing a thoughtful take after reading the source.",
    },
    {
        "id": "stronger-hook",
        "label": "Stronger Hook",
        "instruction": "Make the opening hook stronger and more scroll-stopping without sounding salesy, breathless, or overhyped.",
    },
    {
        "id": "shorter-post",
        "label": "120-180 Words",
        "instruction": "Tighten this into a concise LinkedIn post in the 120 to 180 word range while preserving the strongest insight.",
    },
    {
        "id": "operator-lesson",
        "label": "Operator Lesson",
        "instruction": "Turn this into a practical operator lesson with a clearer takeaway for founders, product, or engineering leaders.",
    },
    {
        "id": "more-opinionated",
        "label": "More Opinionated",
        "instruction": "Make this more opinionated and memorable while staying credible, grounded, and factually faithful to the source.",
    },
]

# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _normalize_hashtag(value: str) -> str:
    cleaned = re.sub(r"^#+", "", str(value or "").strip())
    cleaned = re.sub(r"[^A-Za-z0-9]", "", cleaned)
    return f"#{cleaned}" if cleaned else ""


def _build_full_post(draft: dict) -> str:
    sections = [_clean(draft.get("hook", ""))]
    sections += [_clean(p) for p in draft.get("bodyParagraphs", []) if p]
    sections.append(_clean(draft.get("cta", "")))
    sections = [s for s in sections if s]
    hashtags = " ".join(
        h for h in (_normalize_hashtag(h) for h in draft.get("hashtags", [])) if h
    )
    parts = ["\n\n".join(sections)]
    if hashtags:
        parts.append(hashtags)
    return "\n\n".join(p for p in parts if p).strip()


# ---------------------------------------------------------------------------
# Keyword-based metadata derivation
# ---------------------------------------------------------------------------

def _derive_pillar(text: str) -> dict:
    t = text.lower()
    if re.search(r"cache|memory|quant|compression|latency|throughput|serving|gpu|inference", t):
        return {"id": "ai-efficiency", "label": "AI Efficiency"}
    if re.search(r"product|copilot|workflow|roadmap|adoption|enterprise", t):
        return {"id": "product-strategy", "label": "Product Strategy"}
    if re.search(r"lesson|takeaway|what i learned|operator|playbook", t):
        return {"id": "engineering-lessons", "label": "Engineering Lessons"}
    if re.search(r"founder|market|pricing|distribution|customer|go-to-market", t):
        return {"id": "founder-updates", "label": "Founder Updates"}
    return {"id": "operator-commentary", "label": "Operator Commentary"}


def _build_keyword_hashtags(text: str) -> list[str]:
    t = text.lower()
    tags: list[str] = []
    if re.search(r"llm|language model|inference|prompt|cache|quant", t):
        tags += ["#LLM", "#AIInfrastructure"]
    if re.search(r"research|paper|blog|benchmark", t):
        tags.append("#AIResearch")
    if re.search(r"speed|latency|memory|efficiency|compression", t):
        tags += ["#Efficiency", "#MachineLearning"]
    if not tags:
        tags = ["#AI", "#Technology", "#Product"]
    return list(dict.fromkeys(tags))[:5]


def _build_library_tags(text: str, source_type: str) -> list[str]:
    t = text.lower()
    tags: list[str] = []
    if re.search(r"llm|language model|kv cache|inference|model serving|token", t):
        tags += ["llm", "inference"]
    if re.search(r"memory|compression|quant|latency|throughput|efficiency", t):
        tags.append("efficiency")
    if re.search(r"research|paper|benchmark|blog", t):
        tags.append("research")
    if re.search(r"product|copilot|adoption|enterprise|roadmap", t):
        tags.append("product")
    if re.search(r"founder|go-to-market|distribution|customer|pricing", t):
        tags.append("founder")
    if source_type == "x-post":
        tags.append("x-sourced")
    return list(dict.fromkeys(tags))[:6]


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

_DRAFT_SYSTEM_PROMPT = (
    "You write polished LinkedIn posts from source material. Write as the user in first person "
    "as an independent creator. The source is something the user read or saw, not something they "
    "authored. Keep it specific, practical, and brand-building. Avoid hype, avoid corporate-speak, "
    "and do not invent facts beyond the source text and provided media notes. Never imply the user "
    "is the original source account or affiliated with it. Use natural wording like "
    "'I came across a post from...' instead of awkward phrasing. Make every paragraph a complete "
    "thought. Do not use placeholders like '(link)'. "
    "Respond ONLY with a JSON object with these fields: "
    "headline (string), hook (string), bodyParagraphs (array of 2-4 strings), "
    "cta (string), hashtags (array of 2-5 strings)."
)

_REWRITE_SYSTEM_PROMPT = (
    "You rewrite LinkedIn posts for a personal brand. Write as the user in first person as an "
    "independent creator. The source is something the user saw or read, not something they authored. "
    "Apply the rewrite instructions precisely, keep the draft specific and practical, and never imply "
    "the user is the source account or affiliated with it. Avoid placeholders and make each paragraph "
    "a complete thought. "
    "Respond ONLY with a JSON object with these fields: "
    "headline (string), hook (string), bodyParagraphs (array of 2-4 strings), "
    "cta (string), hashtags (array of 2-5 strings)."
)


def _build_model_prompt(source: dict, voice: str) -> str:
    lines = list(WRITER_CONTEXT) + [
        "",
        "Goal: write a LinkedIn post that helps the user build a thoughtful personal brand from a sourced insight.",
        f"Voice: {voice}",
        f"Voice guide: {VOICE_GUIDES[voice]}",
        f"Source type: {source.get('type', 'manual')}",
        f"Author: {source.get('author_name') or source.get('author_handle') or 'Unknown'}",
        f"Media notes: {source.get('manual_media_notes') or 'None'}",
        f"Source text:\n{source.get('text', '')}",
    ]
    return "\n\n".join(lines)


def _build_rewrite_prompt(source: dict, current_draft: dict, voice: str, instructions: str) -> str:
    lines = list(WRITER_CONTEXT) + [
        "",
        "Goal: rewrite an existing LinkedIn draft so it better fits the user's personal brand.",
        f"Voice: {voice}",
        f"Voice guide: {VOICE_GUIDES[voice]}",
        f"Rewrite instructions: {instructions}",
        f"Source author: {source.get('author_name') or source.get('author_handle') or 'Unknown'}",
        f"Source type: {source.get('type', 'manual')}",
        f"Source text:\n{source.get('text', '')}",
        f"Current headline: {current_draft.get('headline', '')}",
        f"Current hook: {current_draft.get('hook', '')}",
        f"Current draft body:\n{current_draft.get('fullPost', '')}",
        "Write in first person as an independent creator reacting to something you came across.",
        "Do not impersonate or speak on behalf of the source account.",
        "Apply the rewrite instructions directly, not as meta commentary.",
    ]
    return "\n\n".join(lines)


def _polish(text: str, source: dict) -> str:
    text = _clean(text)
    author = source.get("author_name") or source.get("author_handle") or ""
    if author:
        text = re.sub(
            rf"\bI saw {re.escape(author)} post\b",
            f"I came across a post from {author}",
            text, flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"\bI saw a post from {re.escape(author)}\b",
            f"I came across a post from {author}",
            text, flags=re.IGNORECASE,
        )
    text = re.sub(r"\((?:link|source)\)", "", text, flags=re.IGNORECASE)
    return _clean(text)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_llm(prompt: str, system: str, task_name: str) -> dict:
    """
    Call the LLM and return parsed JSON draft fields.
    Raises on any failure — no fallback.
    """
    import time
    from core.llm_client import call_with_free_model_retry, create_llm_client, get_model_name
    from core.llmops import record_llm_call

    client = create_llm_client()
    model = get_model_name()
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        response = call_with_free_model_retry(
            lambda: client.messages.create(
                model=model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            ),
            model,
        )
    except Exception as exc:
        record_llm_call(
            task=task_name,
            model=model,
            status="api_error",
            started_at=started_at,
            latency_ms=(time.monotonic() - started) * 1000,
            error=str(exc),
        )
        raise RuntimeError(f"LLM call failed for {task_name}: {exc}") from exc

    text = "".join(
        getattr(b, "text", "")
        for b in getattr(response, "content", [])
        if getattr(b, "type", None) == "text"
    )
    # Strip markdown fences
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()

    record_llm_call(
        task=task_name,
        model=model,
        status="ok",
        started_at=started_at,
        latency_ms=(time.monotonic() - started) * 1000,
    )

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM returned invalid JSON for {task_name}: {exc}") from exc


def _parse_draft_response(raw: dict, source: dict, mode: str) -> dict:
    draft = {
        "headline": _clean(raw.get("headline", "")),
        "hook": _polish(raw.get("hook", ""), source),
        "bodyParagraphs": [
            _polish(p, source)
            for p in (raw.get("bodyParagraphs") or [])
            if p
        ],
        "cta": _polish(raw.get("cta", ""), source),
        "hashtags": [
            h for h in (_normalize_hashtag(h) for h in (raw.get("hashtags") or [])) if h
        ],
        "generation": {
            "mode": mode,
            "provider": "openrouter",
            "model": settings.OPENROUTER_MODEL,
        },
    }
    draft["fullPost"] = _build_full_post(draft)
    return draft


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_voice(voice: str | None) -> str:
    v = str(voice or DEFAULT_VOICE).strip().lower()
    return v if v in VOICE_GUIDES else DEFAULT_VOICE


def build_enqueue_payload(
    *,
    text: str,
    author: str = "",
    source_url: str = "",
    voice: str | None = None,
    origin: str = "telegram",
    rewrite_of: str = "",
    rewrite_instructions: str = "",
    preset_id: str = "",
) -> dict:
    """
    Build the payload dict to pass to sqlite_store.enqueue().
    Does NOT call the LLM. Safe to call synchronously in the Telegram handler.
    Returns a dict shaped to match what sqlite_store.enqueue() expects.
    """
    voice = normalize_voice(voice)
    text = _clean(text)
    if not text:
        raise ValueError("Source text is required.")

    source_type = "x-post" if _clean(source_url) else "manual"
    pillar = _derive_pillar(text)
    tags = _build_library_tags(text, source_type)
    draft_id = str(uuid.uuid4())

    return {
        "id": draft_id,
        "voice": voice,
        "origin": origin,
        "source": {
            "text": text,
            "author_name": _clean(author),
            "url": _clean(source_url),
            "type": source_type,
        },
        "library": {
            "pillar": pillar,
            "tags": tags,
            "parent_draft_id": _clean(rewrite_of),
            "rewrite_instructions": _clean(rewrite_instructions),
            "preset_id": _clean(preset_id),
        },
    }


def generate_draft(row: dict, parent_draft: dict | None = None) -> dict:
    """
    LLM-generate a LinkedIn post from a SQLite row.

    Args:
        row:          SQLite row dict from sqlite_store (flat fields).
        parent_draft: the draft dict of the parent record (for rewrites only).

    Returns:
        draft dict with keys: headline, hook, bodyParagraphs, cta, hashtags,
        fullPost, generation.

    Raises RuntimeError on LLM failure — caller marks the row failed.
    """
    voice = row.get("voice", DEFAULT_VOICE)
    source_text = row.get("source_text", "")
    source_author = row.get("source_author", "")
    source_url = row.get("source_url", "")
    source_type = row.get("source_type", "manual")

    source = {
        "text": source_text,
        "author_name": source_author,
        "url": source_url,
        "type": source_type,
    }

    rewrite_instructions = row.get("rewrite_instructions", "")
    preset_id = row.get("preset_id", "")
    rewrite_of = row.get("rewrite_of", "")

    # Resolve preset instruction
    if preset_id and not rewrite_instructions:
        preset = next((p for p in REWRITE_PRESETS if p["id"] == preset_id), None)
        if preset:
            rewrite_instructions = preset["instruction"]

    is_rewrite = bool(rewrite_of and (rewrite_instructions or preset_id))

    if is_rewrite and parent_draft:
        prompt = _build_rewrite_prompt(source, parent_draft, voice, rewrite_instructions)
        raw = _call_llm(prompt, _REWRITE_SYSTEM_PROMPT, "linkedin_rewrite")
        draft = _parse_draft_response(raw, source, "model-rewrite")
    else:
        prompt = _build_model_prompt(source, voice)
        raw = _call_llm(prompt, _DRAFT_SYSTEM_PROMPT, "linkedin_draft")
        draft = _parse_draft_response(raw, source, "model")

    record_activity(
        event="linkedin_draft_generated",
        component="linkedin",
        summary="Generated LinkedIn draft via LLM",
        metadata={"draft_id": row.get("id", "")[:8], "voice": voice},
    )
    return draft


def format_queued_for_telegram(row: dict) -> str:
    """Confirmation message shown immediately after queueing."""
    draft_id = row.get("id", "")[:8]
    voice = row.get("voice", DEFAULT_VOICE)
    pillar = row.get("pillar_label", "") or _derive_pillar(row.get("source_text", "")).get("label", "")
    source = row.get("source_author", "") or row.get("source_url", "") or "manual text"
    return (
        f"⏳ *Draft queued* · `{draft_id}`\n"
        f"Voice: {voice} · Pillar: {pillar}\n"
        f"Source: {source}\n\n"
        f"Processing runs every 15 min. You'll be notified when it's ready."
    )


def format_ready_for_telegram(row: dict, draft: dict) -> str:
    """Full post notification sent when processing completes."""
    draft_id = row.get("id", "")[:8]
    voice = row.get("voice", DEFAULT_VOICE)
    pillar = row.get("pillar_label", "")
    generation_mode = draft.get("generation", {}).get("mode", "")
    source_author = row.get("source_author", "")
    drive_filename = row.get("drive_filename", "")
    headline = draft.get("headline", "Untitled")
    full_post = draft.get("fullPost", "")

    lines = [
        f"✅ *{headline}*",
        f"_Voice: {voice} · Pillar: {pillar} · Mode: {generation_mode}_",
    ]
    if source_author:
        lines.append(f"_Source: {source_author}_")
    lines.append("")
    lines.append(full_post)
    lines.append("")
    if drive_filename:
        lines.append(f"_Saved as `{drive_filename}` in Drive_")
    lines.append(f"_Draft ID: `{draft_id}`_")
    return "\n".join(lines)


def format_failed_for_telegram(row: dict) -> str:
    """Error notification sent when all retry attempts are exhausted."""
    draft_id = row.get("id", "")[:8]
    attempts = row.get("attempts", 0)
    error = row.get("last_error", "unknown error")
    return (
        f"❌ *LinkedIn draft failed permanently*\n"
        f"ID: `{draft_id}` · Attempts: {attempts}\n"
        f"Error: {error[:200]}"
    )
