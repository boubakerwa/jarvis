"""
Marvis — entry point.
Starts the Telegram bot (main thread) and Gmail watcher (background thread).
"""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_GMAIL_ACTIVITY_FILE = "data/gmail_activity.jsonl"


@dataclass
class EmailProcessingResult:
    outcome: str
    reason: str = ""
    filed_count: int = 0
    failed_count: int = 0


def _trim(text: str, max_len: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[: max_len - 3]}..."


def _format_email_summary_message(email, result: EmailProcessingResult) -> str:
    outcome_labels = {
        "skipped": "skipped",
        "no_attachments": "no attachments",
        "filed": "filed",
        "partial": "partial",
        "failed": "failed",
    }
    lines = [
        f"[Gmail] Email {outcome_labels.get(result.outcome, result.outcome)}",
        f"Subject: {_trim(email.subject or '(no subject)', 120)}",
        f"From: {_trim(email.sender or '(unknown sender)', 120)}",
    ]
    if result.reason:
        lines.append(f"Reason: {_trim(result.reason, 180)}")
    if result.outcome == "filed":
        lines.append(f"Attachments: {result.filed_count} filed")
    elif result.outcome == "partial":
        lines.append(f"Attachments: {result.filed_count} filed, {result.failed_count} failed")
    elif result.outcome == "failed":
        lines.append(f"Attachments: {result.failed_count} failed")
    elif result.outcome == "no_attachments":
        lines.append("Attachments: none")
    return "\n".join(lines)


def _format_batch_summary(results: list[tuple]) -> str:
    """Build a single aggregated Telegram message for a poll-cycle batch of emails."""
    if not results:
        return ""

    counts: dict[str, int] = {}
    lines = [f"[Gmail] {len(results)} email(s) processed"]

    for email, result, exc in results:
        subject = _trim(email.subject or "(no subject)", 80)
        sender = _trim(email.sender or "(unknown)", 60)

        if exc is not None:
            outcome_label = "error"
            detail = _trim(str(exc), 100)
        else:
            outcome_label = result.outcome
            detail = None

        counts[outcome_label] = counts.get(outcome_label, 0) + 1

        icon = {"filed": "✅", "partial": "⚠️", "skipped": "⏭", "no_attachments": "📭", "failed": "❌", "error": "🔴"}.get(outcome_label, "•")
        line = f"{icon} {subject} — {sender}"
        if detail:
            line += f"\n   ↳ {detail}"
        elif result and result.reason and outcome_label in ("skipped", "no_attachments"):
            line += f"\n   ↳ {_trim(result.reason, 100)}"
        lines.append(line)

    summary_parts = [f"{v} {k}" for k, v in counts.items()]
    lines.insert(1, "(" + ", ".join(summary_parts) + ")")

    return "\n".join(lines)


def _format_email_failure_message(email, error: Exception) -> str:
    lines = [
        "[Gmail] Email processing error",
        f"Subject: {_trim(email.subject or '(no subject)', 120)}",
        f"From: {_trim(email.sender or '(unknown sender)', 120)}",
        f"Error: {_trim(str(error), 180)}",
    ]
    return "\n".join(lines)


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


def _handle_email(email, memory_manager, drive_client) -> EmailProcessingResult:
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
            return EmailProcessingResult(outcome="skipped", reason=reason)

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
            return EmailProcessingResult(outcome="no_attachments", reason=reason)

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
            return EmailProcessingResult(
                outcome="partial",
                reason=reason,
                filed_count=len(filed_attachments),
                failed_count=len(failed_attachments),
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
            return EmailProcessingResult(
                outcome="filed",
                reason=reason,
                filed_count=len(filed_attachments),
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
            return EmailProcessingResult(
                outcome="failed",
                reason=reason,
                failed_count=len(failed_attachments),
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

    from telegram_bot.bot import TelegramBot, TelegramProactiveNotifier
    bot = TelegramBot(
        agent=agent,
        memory_manager=memory_manager,
        drive_client=drive_client,
        calendar_client=calendar_client,
        notes_manager=notes_manager,
    )
    proactive_notifier = TelegramProactiveNotifier(
        enabled=settings.TELEGRAM_EMAIL_SUMMARY_NOTIFICATIONS,
    )
    record_audit(event="telegram_ready", component="telegram", summary="Telegram bot initialised")

    # LinkedIn processor cron (every 15 minutes)
    def _linkedin_cron_loop() -> None:
        import time as _time
        from linkedin.processor import process_pending_drafts
        _INTERVAL = 15 * 60
        logger.info("LinkedIn processor cron started (interval: %ds)", _INTERVAL)
        while True:
            _time.sleep(_INTERVAL)
            try:
                process_pending_drafts(notes_manager, notifier=proactive_notifier)
            except Exception as _exc:
                logger.exception("LinkedIn processor cron error: %s", _exc)

    linkedin_cron_thread = threading.Thread(
        target=_linkedin_cron_loop, daemon=True, name="linkedin-processor"
    )
    linkedin_cron_thread.start()
    record_activity(
        event="linkedin_processor_started",
        component="linkedin",
        summary="LinkedIn processor cron started (15 min interval)",
    )

    # Morning digest (daily scheduled message)
    if settings.JARVIS_MORNING_DIGEST_ENABLED:
        from morning_digest import MorningDigestRunner
        morning_runner = MorningDigestRunner(notifier=proactive_notifier)
        morning_thread = threading.Thread(
            target=morning_runner.run_forever, daemon=True, name="morning-digest"
        )
        morning_thread.start()
        record_activity(
            event="morning_digest_started",
            component="morning_digest",
            summary=f"Morning digest scheduled for {settings.JARVIS_MORNING_TIME} local time",
        )
    else:
        logger.info("Morning digest disabled (set JARVIS_MORNING_DIGEST_ENABLED=true to enable)")

    # Gmail watcher (background thread)
    from gmail.watcher import GmailWatcher

    # Per-email: process and stash result; no Telegram message yet.
    _email_results: list[tuple] = []  # (email, result | None, exc | None)

    def email_callback(email):
        try:
            result = _handle_email(email, memory_manager, drive_client)
            _email_results.append((email, result, None))
        except Exception as exc:
            _email_results.append((email, None, exc))
            raise

    # Batch: send one aggregated summary after all emails in the poll cycle are processed.
    def batch_callback(emails):
        results = _email_results[-len(emails):]  # grab the matching tail
        proactive_notifier.send_message(_format_batch_summary(results))

    watcher = GmailWatcher(on_email=email_callback, on_batch=batch_callback)
    gmail_thread = threading.Thread(target=watcher.run_forever, daemon=True, name="gmail-watcher")
    gmail_thread.start()
    logger.info("Gmail watcher started in background thread")
    record_audit(event="gmail_ready", component="gmail", summary="Gmail watcher started")

    # Telegram bot (blocks main thread)
    bot.run()


if __name__ == "__main__":
    main()
