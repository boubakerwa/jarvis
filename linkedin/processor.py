"""
LinkedIn draft processor.

Reads pending rows from SQLite, calls the LLM, writes the finished artefact
to Obsidian (Marvis/LinkedIn/), updates SQLite, notifies via Telegram.

Runs on a 15-minute cron from main.py.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.opslog import record_activity, record_issue

if TYPE_CHECKING:
    from notes.service import NotesManager
    from telegram_bot.bot import TelegramProactiveNotifier

logger = logging.getLogger(__name__)


def process_pending_drafts(
    notes_manager: "NotesManager | None",
    notifier: "TelegramProactiveNotifier | None" = None,
) -> dict:
    """
    Pick up all pending drafts from SQLite, generate via LLM, write to Obsidian.

    Returns: {processed, failed, skipped, errors}
    """
    from linkedin.sqlite_store import list_pending, mark_ready, mark_attempt_failed
    from linkedin.composer import generate_draft, format_ready_for_telegram, format_failed_for_telegram
    from linkedin.obsidian_store import save_artefact

    summary: dict = {"processed": 0, "failed": 0, "skipped": 0, "errors": []}

    try:
        pending = list_pending()
    except Exception as exc:
        msg = f"LinkedIn processor: failed to read pending queue: {exc}"
        logger.error(msg)
        record_issue(
            level="ERROR",
            event="linkedin_processor_list_failed",
            component="linkedin",
            status="error",
            summary=msg,
        )
        summary["errors"].append(msg)
        return summary

    if not pending:
        logger.debug("LinkedIn processor: no pending drafts")
        record_activity(
            event="linkedin_processor_ran",
            component="linkedin",
            summary="LinkedIn processor ran — no pending drafts",
        )
        return summary

    logger.info("LinkedIn processor: %d pending draft(s)", len(pending))

    for row in pending:
        draft_id = row.get("id", "unknown")[:8]
        parent_draft = None

        # For rewrites, load the parent draft content from Obsidian if available
        rewrite_of = row.get("rewrite_of", "")
        if rewrite_of and notes_manager:
            try:
                from linkedin.sqlite_store import get_by_id
                parent_row = get_by_id(rewrite_of)
                if parent_row and parent_row.get("obsidian_path"):
                    note_content = notes_manager.read_note(parent_row["obsidian_path"])
                    # Parse just enough to reconstruct a draft dict for the rewrite prompt
                    parent_draft = {"fullPost": note_content, "headline": parent_row.get("obsidian_filename", "")}
            except Exception as exc:
                logger.warning("Could not load parent draft %s for rewrite: %s", rewrite_of[:8], exc)

        try:
            draft = generate_draft(row, parent_draft=parent_draft)

            # Save artefact to Obsidian
            obsidian_path, note_title = save_artefact(notes_manager, row, draft)

            mark_ready(row["id"], obsidian_path=obsidian_path, obsidian_filename=note_title)
            summary["processed"] += 1

            # Fetch updated row for notification (has obsidian_filename set)
            from linkedin.sqlite_store import get_by_id
            updated_row = get_by_id(row["id"]) or row
            updated_row["obsidian_filename"] = note_title

            record_activity(
                event="linkedin_draft_processed",
                component="linkedin",
                summary=f"LinkedIn draft {draft_id} generated and saved",
                metadata={"draft_id": draft_id, "note": obsidian_path},
            )

            if notifier:
                notifier.send_message(format_ready_for_telegram(updated_row, draft))

        except Exception as exc:
            error_str = str(exc)
            logger.warning("LinkedIn processor: draft %s failed: %s", draft_id, error_str)
            summary["errors"].append(f"{draft_id}: {error_str}")

            permanent = isinstance(exc, (ImportError, RuntimeError)) and "cannot import" in error_str
            new_status = mark_attempt_failed(row["id"], error_str, permanent=permanent)

            record_issue(
                level="WARNING",
                event="linkedin_draft_generation_failed",
                component="linkedin",
                status="warning",
                summary=f"LinkedIn draft {draft_id} generation failed → {new_status}",
                metadata={"draft_id": draft_id, "error": error_str},
            )

            if new_status == "failed":
                summary["failed"] += 1
                if notifier:
                    from linkedin.sqlite_store import get_by_id
                    failed_row = get_by_id(row["id"]) or row
                    notifier.send_message(format_failed_for_telegram(failed_row))

    record_activity(
        event="linkedin_processor_ran",
        component="linkedin",
        summary=(
            f"LinkedIn processor: {summary['processed']} processed, "
            f"{summary['failed']} failed, {summary['skipped']} skipped"
        ),
        metadata=summary,
    )
    return summary
