"""
Jarvis — entry point.
Starts the Telegram bot (main thread) and Gmail watcher (background thread).
"""
import logging
import os
import threading

# Ensure data/ and logs/ directories exist before anything else
os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/jarvis.log"),
    ],
)
logger = logging.getLogger(__name__)


def _handle_email(email, memory_manager, drive_client):
    """Process a new email: classify attachments and file to Drive."""
    from agent_sdk.filer import classify_attachment
    from memory.schema import MemoryCategory, MemoryConfidence, MemoryRecord, MemorySource

    logger.info(
        "Processing email: from=%s subject=%s attachments=%d",
        email.sender,
        email.subject,
        len(email.attachments),
    )

    for attachment in email.attachments:
        try:
            classification = classify_attachment(
                attachment.filename,
                attachment.mime_type,
                attachment.text_content,
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
            logger.info(
                "Filed attachment '%s' -> %s/%s (Drive ID: %s)",
                attachment.filename,
                classification.top_level,
                classification.sub_folder,
                drive_file_id,
            )
        except Exception:
            logger.exception("Failed to file attachment: %s", attachment.filename)


def main():
    logger.info("Starting Jarvis...")

    # Memory
    from memory.manager import MemoryManager
    memory_manager = MemoryManager()
    logger.info("Memory manager initialised (%d memories)", memory_manager.count())

    # Drive
    from storage.drive import DriveClient
    drive_client = DriveClient()
    drive_client.init_drive_structure()
    logger.info("Drive client initialised")

    # Agent
    from core.agent import JarvisAgent
    agent = JarvisAgent(memory_manager=memory_manager, drive_client=drive_client)
    logger.info("Agent initialised")

    # Gmail watcher (background thread)
    from gmail.watcher import GmailWatcher

    def email_callback(email):
        _handle_email(email, memory_manager, drive_client)

    watcher = GmailWatcher(on_email=email_callback)
    gmail_thread = threading.Thread(target=watcher.run_forever, daemon=True, name="gmail-watcher")
    gmail_thread.start()
    logger.info("Gmail watcher started in background thread")

    # Telegram bot (blocks main thread)
    from telegram.bot import TelegramBot
    bot = TelegramBot(agent=agent, memory_manager=memory_manager, drive_client=drive_client)
    bot.run()


if __name__ == "__main__":
    main()
