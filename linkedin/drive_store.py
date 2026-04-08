"""
LinkedIn Drive artefact store — write-only.

Uploads the finished LinkedIn post as a Markdown file to:
  Jarvis/PR/LinkedIn Composer/<YYYY-MM>_<slug>_<id8>.md

This file is the human-readable artefact — what you'd open in Drive to copy-paste
into LinkedIn. No processing state, no retries, no JSON blobs.

All queue/status tracking lives in SQLite (linkedin/sqlite_store.py).
"""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from googleapiclient.http import MediaIoBaseUpload

if TYPE_CHECKING:
    from storage.drive import DriveClient

logger = logging.getLogger(__name__)

LINKEDIN_TOP_LEVEL = "PR"
LINKEDIN_SUB_FOLDER = "LinkedIn Composer"


def _slug(text: str, max_len: int = 48) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return cleaned[:max_len] or "linkedin_post"


def _make_filename(draft_id: str, headline: str) -> str:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    slug = _slug(headline) if headline else draft_id[:8]
    return f"{month}_{slug}_{draft_id[:8]}.md"


def _build_markdown(row: dict, draft: dict) -> str:
    """
    Render the finished artefact as clean Markdown.
    Contains only the post content + metadata front-matter — nothing personal.
    """
    headline = draft.get("headline", "Untitled")
    full_post = draft.get("fullPost", "")
    voice = row.get("voice", "professional")
    pillar = row.get("pillar_label", "")
    source_author = row.get("source_author", "")
    source_url = row.get("source_url", "")
    source_type = row.get("source_type", "manual")
    model = draft.get("generation", {}).get("model", "")
    created = row.get("created_at", "")[:19].replace("T", " ")

    lines = [
        "---",
        f"headline: {headline}",
        f"voice: {voice}",
        f"pillar: {pillar}",
        f"source_type: {source_type}",
    ]
    if source_author:
        lines.append(f"source_author: {source_author}")
    if source_url:
        lines.append(f"source_url: {source_url}")
    if model:
        lines.append(f"model: {model}")
    lines += [f"created: {created}", "---", "", f"# {headline}", "", full_post]
    return "\n".join(lines)


def upload_artefact(drive: "DriveClient", row: dict, draft: dict) -> tuple[str, str]:
    """
    Upload the finished LinkedIn post as a .md file to Drive.

    Args:
        drive: authenticated DriveClient
        row:   SQLite row dict for the draft
        draft: the generated draft dict (from composer.generate_draft)

    Returns:
        (drive_file_id, filename)
    """
    draft_id = row.get("id", "unknown")
    headline = draft.get("headline", "")
    filename = _make_filename(draft_id, headline)

    folder_id = drive.get_or_create_folder_path(LINKEDIN_TOP_LEVEL, LINKEDIN_SUB_FOLDER)
    content = _build_markdown(row, draft)
    data = content.encode("utf-8")

    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="text/markdown", resumable=False)
    metadata = {"name": filename, "parents": [folder_id]}
    result = drive._service.files().create(
        body=metadata, media_body=media, fields="id"
    ).execute()
    file_id = str(result["id"])

    logger.info(
        "LinkedIn artefact uploaded: %s → Drive %s (file=%s)",
        draft_id[:8], filename, file_id,
    )
    return file_id, filename
