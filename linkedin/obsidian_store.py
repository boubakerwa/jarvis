"""
LinkedIn Obsidian artefact store.

Saves finished LinkedIn posts as Markdown notes under:
  Marvis/LinkedIn/<YYYY-MM>/<slug>_<id8>.md

The note contains YAML front-matter (metadata) + the clean post body.
No personal inferences. No processing state. Just the post.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from notes.service import NotesManager

logger = logging.getLogger(__name__)

LINKEDIN_FOLDER = "LinkedIn"


def _slug(text: str, max_len: int = 48) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return cleaned[:max_len] or "linkedin_post"


def _make_note_title(draft_id: str, headline: str) -> str:
    slug = _slug(headline) if headline else draft_id[:8]
    return f"{slug}_{draft_id[:8]}"


def _make_folder(subfolder: bool = True) -> str:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if subfolder:
        return f"{LINKEDIN_FOLDER}/{month}"
    return LINKEDIN_FOLDER


def _build_note_body(row: dict, draft: dict) -> str:
    headline = draft.get("headline", "Untitled")
    full_post = draft.get("fullPost", "")
    hook = draft.get("hook", "")
    hashtags = draft.get("hashtags", [])
    voice = row.get("voice", "professional")
    pillar = row.get("pillar_label", "")
    source_author = row.get("source_author", "")
    source_url = row.get("source_url", "")
    source_type = row.get("source_type", "manual")
    model = draft.get("generation", {}).get("model", "")
    created = row.get("created_at", "")[:10]
    rewrite_of = row.get("rewrite_of", "")

    # YAML front-matter
    fm_lines = [
        "---",
        f"title: \"{headline}\"",
        f"voice: {voice}",
        f"pillar: {pillar}",
        f"source_type: {source_type}",
    ]
    if source_author:
        fm_lines.append(f"source_author: \"{source_author}\"")
    if source_url:
        fm_lines.append(f"source_url: \"{source_url}\"")
    if rewrite_of:
        fm_lines.append(f"rewrite_of: {rewrite_of[:8]}")
    if model:
        fm_lines.append(f"model: {model}")
    fm_lines.append(f"date: {created}")
    if hashtags:
        tags_yaml = ", ".join(h.lstrip("#") for h in hashtags)
        fm_lines.append(f"tags: [{tags_yaml}]")
    fm_lines.append("---")

    body_lines = [
        "",
        f"# {headline}",
        "",
        full_post,
    ]

    return "\n".join(fm_lines) + "\n" + "\n".join(body_lines)


def save_artefact(
    notes_manager: "NotesManager | None",
    row: dict,
    draft: dict,
) -> tuple[str, str]:
    """
    Save the finished LinkedIn post to Obsidian.

    Returns (note_path, note_title).
    If notes_manager is None, returns placeholder strings and logs a warning.
    """
    if notes_manager is None:
        logger.warning(
            "LinkedIn artefact: notes_manager not available — "
            "set OBSIDIAN_VAULT_PATH to persist posts to Obsidian"
        )
        return "(obsidian unavailable)", "(obsidian unavailable)"

    draft_id = row.get("id", "unknown")
    headline = draft.get("headline", "")
    title = _make_note_title(draft_id, headline)
    folder = _make_folder()
    body = _build_note_body(row, draft)

    result = notes_manager.create_note(
        title=title,
        body=body,
        folder=folder,
        unique=True,
    )
    note_path = result["path"]

    logger.info(
        "LinkedIn artefact saved to Obsidian: %s → %s",
        draft_id[:8], note_path,
    )
    return note_path, title
