"""
Telegram bot handler. Routes messages to the Jarvis agent loop.
Only processes messages from TELEGRAM_ALLOWED_USER_ID.
"""
import logging
import mimetypes
import tempfile
import os

from telegram import Update, Document, PhotoSize
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import settings

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, agent, memory_manager, drive_client=None):
        self._agent = agent
        self._memory = memory_manager
        self._drive = drive_client
        self._app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
        self._register_handlers()

    def run(self) -> None:
        logger.info("Telegram bot starting (long-poll mode)")
        self._app.run_polling()

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        allow = filters.User(user_id=settings.TELEGRAM_ALLOWED_USER_ID)

        self._app.add_handler(CommandHandler("memories", self._cmd_memories, filters=allow))
        self._app.add_handler(CommandHandler("forget", self._cmd_forget, filters=allow))
        self._app.add_handler(CommandHandler("reset", self._cmd_reset, filters=allow))
        self._app.add_handler(CommandHandler("status", self._cmd_status, filters=allow))

        # File/photo uploads
        self._app.add_handler(
            MessageHandler(allow & filters.Document.ALL, self._handle_document)
        )
        self._app.add_handler(
            MessageHandler(allow & filters.PHOTO, self._handle_photo)
        )

        # Plain text — must be last
        self._app.add_handler(
            MessageHandler(allow & filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _cmd_memories(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        memories = self._memory.list_all()
        if not memories:
            await update.message.reply_text("No memories stored yet.")
            return

        grouped: dict[str, list[str]] = {}
        for m in memories:
            grouped.setdefault(m.category.value.upper(), []).append(
                f"  [{m.topic}] {m.summary}"
            )

        lines = []
        for cat, items in grouped.items():
            lines.append(f"\n*{cat}*")
            lines.extend(items)

        text = "\n".join(lines).strip()
        # Telegram message limit is 4096 chars
        for chunk in _split_message(text, 4096):
            await update.message.reply_text(chunk, parse_mode="Markdown")

    async def _cmd_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        topic = " ".join(context.args) if context.args else ""
        if not topic:
            await update.message.reply_text("Usage: /forget <topic>")
            return
        deleted = self._memory.forget(topic)
        if deleted:
            await update.message.reply_text(f"Forgotten: {topic}")
        else:
            await update.message.reply_text(f"No memory found for: {topic}")

    async def _cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._agent.reset_history()
        await update.message.reply_text("Conversation history cleared. Long-term memories are intact.")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        memory_count = self._memory.count()
        drive_status = "connected" if self._drive else "not initialised"

        lines = [
            f"*Jarvis Status*",
            f"Memories: {memory_count}",
            f"Drive: {drive_status}",
            f"Model: {settings.CLAUDE_MODEL}",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_text = update.message.text
        logger.info("Received message from %s", update.effective_user.id)

        await update.message.chat.send_action("typing")
        try:
            response = self._agent.chat(user_text)
        except Exception as e:
            logger.exception("Agent error")
            response = f"Sorry, something went wrong: {e}"

        for chunk in _split_message(response, 4096):
            await update.message.reply_text(chunk)

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        doc: Document = update.message.document
        await self._file_to_drive(update, context, doc.file_id, doc.file_name, doc.mime_type)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Use highest resolution photo
        photo: PhotoSize = update.message.photo[-1]
        await self._file_to_drive(update, context, photo.file_id, f"photo_{photo.file_id}.jpg", "image/jpeg")

    async def _file_to_drive(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        file_id: str,
        filename: str,
        mime_type: str,
    ) -> None:
        if not self._drive:
            await update.message.reply_text("Drive not initialised, cannot file document.")
            return

        await update.message.reply_text(f"Filing {filename}...")

        try:
            tg_file = await context.bot.get_file(file_id)
            data = await tg_file.download_as_bytearray()

            # Classify via Agent SDK filer
            from agent_sdk.filer import classify_attachment
            from gmail.parser import _extract_text

            text_content = _extract_text(bytes(data), mime_type, filename)
            classification = classify_attachment(filename, mime_type, text_content)

            folder_id = self._drive.get_or_create_folder_path(
                classification.top_level, classification.sub_folder
            )
            drive_file_id = self._drive.upload_bytes(bytes(data), classification.filename, folder_id, mime_type)

            # Store document_ref memory
            from memory.schema import MemoryCategory, MemoryConfidence, MemoryRecord, MemorySource
            record = MemoryRecord(
                topic=f"file:{classification.filename}",
                summary=classification.summary,
                category=MemoryCategory.DOCUMENT_REF,
                source=MemorySource.TELEGRAM,
                confidence=MemoryConfidence.HIGH,
                document_ref=drive_file_id,
            )
            self._memory.upsert(record)

            await update.message.reply_text(
                f"Filed to *{classification.top_level}/{classification.sub_folder}/{classification.filename}*\n"
                f"{classification.summary}",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Failed to file document %s", filename)
            await update.message.reply_text(f"Failed to file document: {e}")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _split_message(text: str, max_len: int) -> list[str]:
    """Split a long message into chunks of max_len characters."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks
