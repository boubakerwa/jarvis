"""
Marvis — entry point.
Starts the Telegram bot (main thread) and Gmail watcher (background thread).
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from time import monotonic
from typing import Optional

from config import settings
from core.opslog import (
    HEARTBEAT_INTERVAL_SECONDS,
    IssuePersistenceHandler,
    new_op_id,
    operation_context,
    record_activity,
    record_audit,
    record_issue,
)

# Ensure data/ and logs/ directories exist before anything else
os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        IssuePersistenceHandler(),
    ],
)
logger = logging.getLogger(__name__)

_GMAIL_ACTIVITY_FILE = "data/gmail_activity.jsonl"


def _record_gmail_activity(email, outcome: str, reason: str = "", details: Optional[dict] = None) -> None:
    payload = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "message_id": email.message_id,
        "thread_id": email.thread_id,
        "from": email.sender,
        "subject": email.subject,
        "date": email.date,
        "attachment_count": len(email.attachments),
        "outcome": outcome,
        "reason": reason,
    }
    if details:
        payload["details"] = details

    os.makedirs(os.path.dirname(os.path.abspath(_GMAIL_ACTIVITY_FILE)), exist_ok=True)
    with open(_GMAIL_ACTIVITY_FILE, "a") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _handle_email(email, memory_manager, drive_client):
    """Process a new email: check relevance, then classify attachments and file to Drive."""
    from agent_sdk.filer import classify_attachment
    from gmail.relevance import is_worth_filing
    from memory.schema import MemoryCategory, MemoryConfidence, MemoryRecord, MemorySource
    from utils.financial_extraction import extract_financial_data

    op_id = new_op_id("email")
    started = monotonic()
    with operation_context(op_id):
        record_activity(
            event="email_processing_started",
            component="gmail",
            summary="Processing incoming email",
            metadata={
                "message_id": email.message_id,
                "attachment_count": len(email.attachments),
            },
        )
        logger.info(
            "Processing email: from=%s subject=%s attachments=%d",
            email.sender,
            email.subject,
            len(email.attachments),
        )

        should_file, reason = is_worth_filing(email)
        if not should_file:
            logger.info("Skipping email (not worth filing): %s — %s", email.subject, reason)
            record_activity(
                event="email_processing_skipped",
                component="gmail",
                status="skipped",
                summary="Email skipped after filing relevance check",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"message_id": email.message_id},
            )
            _record_gmail_activity(email, "skipped", reason)
            return

        logger.info("Filing email: %s — %s", email.subject, reason)
        if not email.attachments:
            logger.info("Email marked worth filing but has no attachments: %s", email.subject)
            record_issue(
                level="WARNING",
                event="email_missing_attachments",
                component="gmail",
                status="warning",
                summary="Email marked for filing had no attachments",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"message_id": email.message_id},
            )
            _record_gmail_activity(email, "no_attachments", reason)
            return

        filed_attachments: list[dict] = []
        failed_attachments: list[dict] = []

        for attachment in email.attachments:
            try:
                classification = classify_attachment(
                    attachment.filename,
                    attachment.mime_type,
                    attachment.text_content,
                    raw_data=attachment.data,
                )
                folder_id = drive_client.get_or_create_folder_path(
                    classification.top_level, classification.sub_folder
                )
                drive_file_id = drive_client.upload_bytes(
                    attachment.data,
                    classification.filename,
                    folder_id,
                    attachment.mime_type,
                )
                record = MemoryRecord(
                    topic=f"file:{classification.filename}",
                    summary=classification.summary,
                    category=MemoryCategory.DOCUMENT_REF,
                    source=MemorySource.EMAIL,
                    confidence=MemoryConfidence.HIGH,
                    document_ref=drive_file_id,
                )
                memory_manager.upsert(record)

                # Extract financial data for finance-classified documents
                if classification.top_level == "Finances" and attachment.text_content:
                    financial = extract_financial_data(attachment.text_content, classification.filename)
                    if financial:
                        memory_manager.add_financial_record(
                            vendor=financial["vendor"],
                            amount=financial["amount"],
                            currency=financial["currency"],
                            category=financial["category"],
                            date=financial["date"],
                            description=classification.summary,
                            drive_file_id=drive_file_id,
                            source="email",
                        )

                logger.info(
                    "Filed attachment '%s' -> %s/%s (Drive ID: %s)",
                    attachment.filename,
                    classification.top_level,
                    classification.sub_folder,
                    drive_file_id,
                )
                filed_attachments.append(
                    {
                        "original_filename": attachment.filename,
                        "stored_filename": classification.filename,
                        "top_level": classification.top_level,
                        "sub_folder": classification.sub_folder,
                        "drive_file_id": drive_file_id,
                    }
                )
            except Exception as e:
                logger.exception("Failed to file attachment: %s", attachment.filename)
                record_issue(
                    level="ERROR",
                    event="email_attachment_filing_failed",
                    component="gmail",
                    status="error",
                    summary="Failed to classify or store email attachment",
                    metadata={
                        "message_id": email.message_id,
                        "filename": attachment.filename,
                        "error": str(e),
                    },
                )
                failed_attachments.append(
                    {
                        "filename": attachment.filename,
                        "error": str(e),
                    }
                )

        duration_ms = (monotonic() - started) * 1000
        if filed_attachments and failed_attachments:
            record_issue(
                level="WARNING",
                event="email_processing_partial",
                component="gmail",
                status="partial",
                summary="Email processing completed with partial failures",
                duration_ms=duration_ms,
                metadata={
                    "message_id": email.message_id,
                    "filed_count": len(filed_attachments),
                    "failed_count": len(failed_attachments),
                },
            )
            _record_gmail_activity(
                email,
                "partial",
                reason,
                {"filed_attachments": filed_attachments, "failed_attachments": failed_attachments},
            )
        elif filed_attachments:
            record_activity(
                event="email_processing_completed",
                component="gmail",
                status="filed",
                summary="Email attachments filed successfully",
                duration_ms=duration_ms,
                metadata={
                    "message_id": email.message_id,
                    "filed_count": len(filed_attachments),
                },
            )
            record_audit(
                event="email_filed",
                component="gmail",
                summary="Stored email attachment(s) in Drive",
                metadata={
                    "message_id": email.message_id,
                    "filed_count": len(filed_attachments),
                },
            )
            _record_gmail_activity(
                email,
                "filed",
                reason,
                {"filed_attachments": filed_attachments},
            )
        else:
            record_issue(
                level="ERROR",
                event="email_processing_failed",
                component="gmail",
                status="failed",
                summary="Email processing failed before any attachment could be stored",
                duration_ms=duration_ms,
                metadata={
                    "message_id": email.message_id,
                    "failed_count": len(failed_attachments),
                },
            )
            _record_gmail_activity(
                email,
                "failed",
                reason,
                {"failed_attachments": failed_attachments},
            )


def _heartbeat_loop() -> None:
    while True:
        record_activity(
            event="app_heartbeat",
            component="runtime",
            summary="Marvis heartbeat",
        )
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


def main():
    logger.info("Starting Marvis...")
    record_activity(event="app_starting", component="runtime", status="starting", summary="Marvis boot sequence started")

    # Memory
    from memory.manager import MemoryManager
    memory_manager = MemoryManager()
    logger.info("Memory manager initialised (%d memories)", memory_manager.count())
    record_audit(event="memory_ready", component="memory", summary="Memory manager initialised")

    # Drive
    from storage.drive import DriveClient
    drive_client = DriveClient()
    drive_client.init_drive_structure()
    logger.info("Drive client initialised")
    record_audit(event="drive_ready", component="drive", summary="Drive client initialised")

    # Calendar
    from calendar_api.client import CalendarClient
    try:
        calendar_client = CalendarClient()
        logger.info("Calendar client initialised")
        record_audit(event="calendar_ready", component="calendar", summary="Calendar client initialised")
    except Exception:
        logger.warning("Calendar client failed to initialise — calendar features disabled")
        record_issue(
            level="WARNING",
            event="calendar_init_failed",
            component="calendar",
            status="warning",
            summary="Calendar client failed to initialise",
        )
        calendar_client = None

    # Notes
    if settings.OBSIDIAN_VAULT_PATH:
        from notes import NotesManager, ObsidianVault

        notes_manager = NotesManager(
            ObsidianVault(
                settings.OBSIDIAN_VAULT_PATH,
                root_folder=settings.OBSIDIAN_ROOT_FOLDER,
            )
        )
        logger.info(
            "Notes workspace initialised (%s/%s)",
            settings.OBSIDIAN_VAULT_PATH,
            settings.OBSIDIAN_ROOT_FOLDER,
        )
        record_audit(event="notes_ready", component="notes", summary="Notes workspace initialised")
    else:
        logger.info("Notes workspace disabled (set OBSIDIAN_VAULT_PATH to enable)")
        notes_manager = None

    # Agent
    from core.agent import JarvisAgent
    agent = JarvisAgent(
        memory_manager=memory_manager,
        drive_client=drive_client,
        calendar_client=calendar_client,
        notes_manager=notes_manager,
    )
    logger.info("Agent initialised")
    record_audit(event="agent_ready", component="agent", summary="Agent initialised")

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="ops-heartbeat")
    heartbeat_thread.start()
    record_activity(event="app_heartbeat_started", component="runtime", summary="Heartbeat thread started")

    # Gmail watcher (background thread)
    from gmail.watcher import GmailWatcher

    def email_callback(email):
        _handle_email(email, memory_manager, drive_client)

    watcher = GmailWatcher(on_email=email_callback)
    gmail_thread = threading.Thread(target=watcher.run_forever, daemon=True, name="gmail-watcher")
    gmail_thread.start()
    logger.info("Gmail watcher started in background thread")
    record_audit(event="gmail_ready", component="gmail", summary="Gmail watcher started")

    # Telegram bot (blocks main thread)
    from telegram_bot.bot import TelegramBot
    bot = TelegramBot(
        agent=agent,
        memory_manager=memory_manager,
        drive_client=drive_client,
        calendar_client=calendar_client,
        notes_manager=notes_manager,
    )
    record_audit(event="telegram_ready", component="telegram", summary="Telegram bot initialised")
    bot.run()


if __name__ == "__main__":
    main()
