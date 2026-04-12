from __future__ import annotations

"""
Telegram bot handler. Routes messages to the Marvis agent loop.
Only processes messages from TELEGRAM_ALLOWED_USER_ID.
"""
import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    PhotoSize,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import settings
from core.llmops import LLM_ACTIVITY_PATH
from core.opslog import (
    HEARTBEAT_INTERVAL_SECONDS,
    OPS_ACTIVITY_PATH,
    OPS_AUDIT_PATH,
    OPS_ISSUES_PATH,
    new_op_id,
    operation_context,
    read_jsonl,
    record_activity,
    record_audit,
    record_issue,
)

logger = logging.getLogger(__name__)


class TelegramProactiveNotifier:
    """Send proactive one-off messages outside the regular chat handler loop."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        bot_token: str | None = None,
        chat_id: int | None = None,
        bot: Bot | None = None,
        max_message_length: int = 4096,
    ):
        self._enabled = enabled
        self._chat_id = chat_id or settings.TELEGRAM_ALLOWED_USER_ID
        self._bot_token = bot_token or settings.TELEGRAM_BOT_TOKEN
        self._bot = bot
        self._max_message_length = max(1, max_message_length)

    def send_message(self, text: str, *, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
        message = text.strip()
        if not self._enabled or not message:
            return False

        try:
            chunks = _split_message(message, self._max_message_length)
            asyncio.run(self._send_chunks(chunks, reply_markup=reply_markup))
            record_activity(
                event="telegram_proactive_message_sent",
                component="telegram",
                summary="Sent proactive Telegram message",
                metadata={
                    "message_length": len(message),
                    "chunk_count": len(chunks),
                    "has_inline_actions": bool(reply_markup),
                },
            )
            return True
        except Exception as exc:
            logger.exception("Failed to send proactive Telegram message")
            record_issue(
                level="ERROR",
                event="telegram_proactive_message_failed",
                component="telegram",
                status="error",
                summary="Failed to send proactive Telegram message",
                metadata={"error": str(exc)},
            )
            return False

    async def _send_chunks(self, chunks: list[str], *, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        if self._bot is not None:
            for index, chunk in enumerate(chunks):
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=chunk,
                    reply_markup=reply_markup if index == len(chunks) - 1 else None,
                )
            return

        # Create a short-lived bot session per proactive send so the underlying
        # HTTP client is initialized and shut down within the same event loop.
        async with Bot(token=self._bot_token) as bot:
            for index, chunk in enumerate(chunks):
                await bot.send_message(
                    chat_id=self._chat_id,
                    text=chunk,
                    reply_markup=reply_markup if index == len(chunks) - 1 else None,
                )


class TelegramBot:
    def __init__(self, agent, memory_manager, drive_client=None, calendar_client=None, notes_manager=None, reminder_manager=None, chat_reset_manager=None, linkedin_drive=None):
        self._agent = agent
        self._memory = memory_manager
        self._drive = drive_client
        self._calendar = calendar_client
        self._notes = notes_manager
        self._reminders = reminder_manager
        self._chat_reset = chat_reset_manager
        self._background_tasks: set[asyncio.Task] = set()
        self._app = (
            Application.builder()
            .token(settings.TELEGRAM_BOT_TOKEN)
            .post_init(self._publish_bot_commands)
            .build()
        )
        self._register_handlers()

    def run(self) -> None:
        logger.info("Telegram bot starting (long-poll mode)")
        record_activity(event="telegram_bot_starting", component="telegram", summary="Telegram bot entering polling mode")
        self._app.run_polling()

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        allow = filters.User(user_id=settings.TELEGRAM_ALLOWED_USER_ID)

        self._app.add_handler(CommandHandler("memories", self._cmd_memories, filters=allow))
        self._app.add_handler(CommandHandler("reminders", self._cmd_reminders, filters=allow))
        self._app.add_handler(CommandHandler("forget", self._cmd_forget, filters=allow))
        self._app.add_handler(CommandHandler("reset", self._cmd_reset, filters=allow))
        self._app.add_handler(CommandHandler("status", self._cmd_status, filters=allow))
        self._app.add_handler(CommandHandler("llmops", self._cmd_llmops, filters=allow))
        self._app.add_handler(CommandHandler("linkedin", self._cmd_linkedin, filters=allow))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback_query, pattern=r"^(reminder|chatreset):"))

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

    def _command_menu(self) -> list[BotCommand]:
        return [
            BotCommand("status", "Show system status"),
            BotCommand("llmops", "Show model usage and costs"),
            BotCommand("memories", "List stored memories"),
            BotCommand("reminders", "List scheduled reminders"),
            BotCommand("forget", "Delete a memory by topic"),
            BotCommand("reset", "Clear chat history"),
            BotCommand("linkedin", "Draft a LinkedIn post from text or URL"),
        ]

    async def _publish_bot_commands(self, application: Application) -> None:
        commands = self._command_menu()
        try:
            await application.bot.set_my_commands(commands)
            await application.bot.set_my_commands(
                commands,
                scope=BotCommandScopeChat(chat_id=settings.TELEGRAM_ALLOWED_USER_ID),
            )
            logger.info("Published %d Telegram bot command(s)", len(commands))
            record_activity(
                event="telegram_commands_published",
                component="telegram",
                summary="Published Telegram bot commands",
                metadata={"command_count": len(commands)},
            )
        except Exception:
            logger.exception("Failed to publish Telegram bot commands")
            record_issue(
                level="ERROR",
                event="telegram_command_publish_failed",
                component="telegram",
                status="error",
                summary="Failed to publish Telegram bot commands",
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

    async def _cmd_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._reminders:
            await update.message.reply_text("Reminder manager not initialised.")
            return

        status = (context.args[0].strip().lower() if context.args else "scheduled")
        allowed_statuses = {"scheduled", "cancelled", "completed", "all"}
        if status not in allowed_statuses:
            await update.message.reply_text("Usage: /reminders [scheduled|cancelled|completed|all]")
            return

        reminders = self._reminders.list_reminders(status)
        if not reminders:
            message = f"No {status} reminders." if status != "all" else "No reminders found."
            await update.message.reply_text(message)
            return

        lines = [f"*Reminders ({status})*"]
        lines.extend(f"- {self._reminders.describe_reminder(reminder)}" for reminder in reminders)
        text = "\n".join(lines)
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
        if self._chat_reset:
            self._chat_reset.reset_session(now=datetime.now(timezone.utc))
        await update.message.reply_text("Conversation history cleared. Long-term memories are intact.")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        memory_count = self._memory.count()
        drive_status = "connected" if self._drive else "not initialised"
        calendar_status = "connected" if self._calendar else "not initialised"
        notes_status = "connected" if self._notes else "not initialised"

        # LinkedIn queue depth
        try:
            from linkedin.sqlite_store import count_by_status
            li_counts = count_by_status()
            li_pending = li_counts.get("pending_generation", 0)
            li_ready = li_counts.get("ready", 0)
            li_failed = li_counts.get("failed", 0)
            li_str = f"{li_pending} pending · {li_ready} ready · {li_failed} failed"
        except Exception:
            li_str = "unavailable"

        lines = [
            "*Marvis Status*",
            f"Memories: {memory_count}",
            f"Drive: {drive_status}",
            f"Calendar: {calendar_status}",
            f"Notes: {notes_status}",
            f"LinkedIn queue: {li_str}",
            f"Model: {settings.OPENROUTER_MODEL}",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_llmops(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        summary = _load_llmops_summary()
        if summary["call_count"] == 0:
            await update.message.reply_text("No LLM activity recorded yet.")
            return

        lines = [
            "*LLMOps*",
            f"Calls: {summary['call_count']}",
            f"Success: {_format_success_rate(summary['success_count'], summary['call_count'])}",
            f"Avg latency: {summary['avg_latency_ms']:.1f} ms",
            f"Tokens: {summary['input_tokens']} in / {summary['output_tokens']} out / {summary['total_tokens']} total",
            f"Estimated cost: {_format_cost(summary['estimated_cost_usd'])} ({summary['priced_call_count']}/{summary['call_count']} priced)",
            f"Models seen: {summary['model_count']}",
            f"Last recorded: {summary['last_recorded_at'] or 'unknown'}",
        ]
        ops = _load_ops_health_summary()
        lines.append(
            f"Ops: heartbeat {ops['heartbeat_status']} ({ops['heartbeat_age_text']}) | issues {ops['issue_count']} | audit {ops['audit_count']}"
        )
        if summary["error_count"]:
            lines.append(f"Issues: {summary['error_count']} failed or validation-error call(s)")
        if summary["top_tasks"]:
            lines.append("")
            lines.append("*Top tasks*")
            for task in summary["top_tasks"]:
                lines.append(
                    f"- {task['task']}: {task['call_count']} calls, {task['total_tokens']} tokens, {task['avg_latency_ms']:.1f} ms avg"
                )

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_linkedin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /linkedin <text>              — queue a draft (confirmed immediately)
        /linkedin voice=X <text>      — set voice: professional|operator|founder
        /linkedin author=@handle <text>
        /linkedin rewrite <id> [preset or instructions]
        /linkedin list [all|pending|ready|failed]
        /linkedin process             — trigger immediate processing run
        /linkedin help

        Nothing from this command is stored in the memory system — ever.
        """
        args_text = " ".join(context.args) if context.args else ""

        if not args_text or args_text.strip().lower() == "help":
            help_text = (
                "*LinkedIn Composer*\n\n"
                "Queue a draft (processed within 15 min):\n"
                "  `/linkedin <text>`\n\n"
                "From an X/Twitter post — just paste the URL:\n"
                "  `/linkedin https://x.com/user/status/123`\n\n"
                "Set voice:\n"
                "  `/linkedin voice=operator <text>`\n"
                "  Voices: professional (default), operator, founder\n\n"
                "Attribute a source:\n"
                "  `/linkedin author=@handle <text>`\n\n"
                "Rewrite a ready draft:\n"
                "  `/linkedin rewrite <draft_id> <preset or instructions>`\n"
                "  Presets: builder-voice, stronger-hook, shorter-post, operator-lesson, more-opinionated\n\n"
                "List drafts:\n"
                "  `/linkedin list` — all recent\n"
                "  `/linkedin list pending` — queued only\n"
                "  `/linkedin list ready` — ready only\n\n"
                "Force-process now:\n"
                "  `/linkedin process`\n\n"
                "_Drafts live in Drive: Jarvis/PR/LinkedIn Composer/_"
            )
            await update.message.reply_text(help_text, parse_mode="Markdown")
            return

        if args_text.strip().lower() == "process":
            await self._linkedin_process_now(update)
            return

        first_word = args_text.strip().split()[0].lower()
        if first_word == "list":
            parts = args_text.strip().split(None, 1)
            status_filter = parts[1].strip().lower() if len(parts) > 1 else None
            valid_filters = {"pending", "ready", "failed", "pending_generation", "all", None}
            if status_filter not in valid_filters:
                status_filter = None
            if status_filter == "pending":
                status_filter = "pending_generation"
            if status_filter == "all":
                status_filter = None
            await self._linkedin_list(update, status_filter=status_filter)
            return

        if first_word == "rewrite":
            remainder = args_text.strip()[len("rewrite"):].strip()
            parts = remainder.split(None, 1)
            draft_id_prefix = parts[0] if parts else ""
            instructions = parts[1] if len(parts) > 1 else ""
            await self._linkedin_rewrite(update, draft_id_prefix, instructions)
            return

        # --- Queue a new draft ---
        voice = "professional"
        author = ""
        remaining = args_text

        voice_match = re.search(r"\bvoice=(professional|operator|founder)\b", remaining, re.IGNORECASE)
        if voice_match:
            voice = voice_match.group(1).lower()
            remaining = remaining.replace(voice_match.group(0), "").strip()

        author_match = re.search(r"\bauthor=(\S+)", remaining, re.IGNORECASE)
        if author_match:
            author = author_match.group(1)
            remaining = remaining.replace(author_match.group(0), "").strip()

        source_input = remaining.strip()
        if not source_input:
            await update.message.reply_text("Please provide source text or an X post URL after /linkedin")
            return

        # Detect a bare X/Twitter URL — fetch tweet content automatically.
        # Any other URL or plain text is treated as manual source text.
        _URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
        source_text = ""
        source_url = ""
        if _URL_RE.match(source_input):
            source_url = source_input
        else:
            source_text = source_input

        op_id = new_op_id("linkedin-queue")
        with operation_context(op_id):
            record_activity(
                event="linkedin_draft_queued",
                component="linkedin",
                summary="LinkedIn draft queued via Telegram",
            )
            try:
                from linkedin.composer import build_enqueue_payload, format_queued_for_telegram
                from linkedin.sqlite_store import enqueue

                payload = build_enqueue_payload(
                    text=source_text,
                    author=author,
                    source_url=source_url,
                    voice=voice,
                    origin="telegram",
                )
                row = enqueue(payload)
                reply = format_queued_for_telegram(row)

                for chunk in _split_message(reply, 4096):
                    await update.message.reply_text(chunk, parse_mode="Markdown")

                record_audit(
                    event="linkedin_draft_queued",
                    component="linkedin",
                    summary="Queued LinkedIn draft to SQLite",
                    metadata={"draft_id": row.get("id", "")[:8], "voice": voice},
                )
            except Exception as exc:
                logger.exception("LinkedIn queue failed")
                record_issue(
                    level="ERROR",
                    event="linkedin_queue_failed",
                    component="linkedin",
                    status="error",
                    summary="Failed to queue LinkedIn draft",
                    metadata={"error": str(exc)},
                )
                await update.message.reply_text(f"Failed to queue draft: {exc}")

    async def _linkedin_list(self, update: Update, status_filter: str | None = None) -> None:
        try:
            from linkedin.sqlite_store import list_drafts
            drafts = list_drafts(limit=12, status_filter=status_filter)
            if not drafts:
                label = f" with status '{status_filter}'" if status_filter else ""
                await update.message.reply_text(f"No LinkedIn drafts{label} found.")
                return

            filter_label = f" · {status_filter}" if status_filter else ""
            lines = [f"*LinkedIn Drafts*{filter_label}\n"]
            for d in drafts:
                draft_id = d.get("id", "")[:8]
                status = d.get("status", "")
                status_icon = {"ready": "✅", "pending_generation": "⏳", "failed": "❌"}.get(status, "•")
                obsidian_note = d.get("obsidian_filename", "") or "_(pending)_"
                created = d.get("created_at", "")[:10]
                voice = d.get("voice", "")
                pillar = d.get("pillar_label", "")
                attempts = d.get("attempts", 0)
                attempt_str = f" · {attempts} attempt(s)" if attempts else ""
                lines.append(
                    f"{status_icon} `{draft_id}` — {obsidian_note}\n"
                    f"  {created} · {voice} · {pillar}{attempt_str}"
                )
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        except Exception as exc:
            logger.exception("LinkedIn list failed")
            await update.message.reply_text(f"Failed to list drafts: {exc}")

    async def _linkedin_rewrite(self, update: Update, draft_id_prefix: str, instructions: str) -> None:
        if not draft_id_prefix:
            await update.message.reply_text("Usage: `/linkedin rewrite <draft_id> <instructions>`", parse_mode="Markdown")
            return

        try:
            from linkedin.sqlite_store import get_by_id_prefix, enqueue
            from linkedin.composer import build_enqueue_payload, format_queued_for_telegram, REWRITE_PRESETS

            existing = get_by_id_prefix(draft_id_prefix)
            if not existing:
                await update.message.reply_text(
                    f"Draft `{draft_id_prefix}` not found. Use `/linkedin list` to see recent drafts.",
                    parse_mode="Markdown",
                )
                return

            if existing.get("status") != "ready":
                await update.message.reply_text(
                    f"Draft `{draft_id_prefix}` is not ready yet (status: `{existing.get('status')}`).",
                    parse_mode="Markdown",
                )
                return

            # Resolve preset
            preset_id = ""
            preset_ids = {p["id"] for p in REWRITE_PRESETS}
            if instructions.strip().lower() in preset_ids:
                preset_id = instructions.strip().lower()
                instructions = ""

            if not instructions and not preset_id:
                preset_list = ", ".join(p["id"] for p in REWRITE_PRESETS)
                await update.message.reply_text(
                    f"Provide rewrite instructions or a preset ID.\nPresets: {preset_list}",
                )
                return

            payload = build_enqueue_payload(
                text=existing.get("source_text", ""),
                author=existing.get("source_author", ""),
                source_url=existing.get("source_url", ""),
                voice=existing.get("voice", "professional"),
                origin="telegram",
                rewrite_of=existing.get("id", ""),
                rewrite_instructions=instructions,
                preset_id=preset_id,
            )
            row = enqueue(payload)
            reply = format_queued_for_telegram(row)
            for chunk in _split_message(reply, 4096):
                await update.message.reply_text(chunk, parse_mode="Markdown")

        except Exception as exc:
            logger.exception("LinkedIn rewrite queue failed")
            await update.message.reply_text(f"Failed to queue rewrite: {exc}")

    async def _linkedin_process_now(self, update: Update) -> None:
        """Trigger an immediate processing run."""
        from linkedin.sqlite_store import list_pending
        pending_count = len(list_pending())
        if pending_count == 0:
            await update.message.reply_text("No pending LinkedIn drafts in queue.")
            return
        await update.message.reply_text(f"⚙️ Processing {pending_count} pending draft(s)…")
        try:
            from linkedin.processor import process_pending_drafts
            summary = process_pending_drafts(self._notes, notifier=None)
            lines = [
                f"✅ Processed: {summary['processed']}",
                f"❌ Failed: {summary['failed']}",
                f"⏭ Skipped: {summary['skipped']}",
            ]
            if summary.get("errors"):
                lines.append("\nErrors:")
                lines += [f"  • {e}" for e in summary["errors"][:5]]
            await update.message.reply_text("\n".join(lines))
        except Exception as exc:
            logger.exception("LinkedIn manual process failed")
            await update.message.reply_text(f"Processor error: {exc}")

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_text = update.message.text
        was_history_empty = self._agent.history_is_empty() if hasattr(self._agent, "history_is_empty") else False
        logger.info("Received message from %s", update.effective_user.id)
        op_id = new_op_id("telegram-message")
        with operation_context(op_id):
            record_activity(
                event="telegram_message_received",
                component="telegram",
                summary="Received Telegram chat message",
                metadata={"update_id": getattr(update, "update_id", 0)},
            )
            await update.message.chat.send_action("typing")
            try:
                response = self._agent.chat(user_text)
            except Exception as e:
                logger.exception("Agent error")
                record_issue(
                    level="ERROR",
                    event="telegram_agent_reply_failed",
                    component="telegram",
                    status="error",
                    summary="Failed to generate Telegram reply",
                    metadata={"error": str(e)},
                )
                response = f"Sorry, something went wrong: {e}"

            response_with_cost = response.rstrip() + "\n\n" + _total_cost_footer()
            for chunk in _split_message(response_with_cost, 4096):
                await update.message.reply_text(chunk)
            if self._chat_reset and was_history_empty:
                session = self._chat_reset.start_session(now=datetime.now(timezone.utc), force_new=True)
                record_activity(
                    event="chat_reset_session_started",
                    component="telegram",
                    summary="Started chat-reset reminder schedule from first message",
                    metadata={"session_id": session["id"]},
                )
            record_activity(
                event="telegram_message_replied",
                component="telegram",
                summary="Sent Telegram chat reply",
                metadata={"chunk_count": len(_split_message(response_with_cost, 4096))},
            )

    async def _handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return

        op_id = new_op_id("telegram-callback")
        with operation_context(op_id):
            await query.answer()
            prefix, action, target_id = _parse_callback_data(query.data or "")
            if prefix == "reminder":
                await self._handle_reminder_callback(query, action, target_id)
                return
            if prefix == "chatreset":
                await self._handle_chat_reset_callback(query, action, target_id)
                return
            else:
                await query.edit_message_text("This reminder action is no longer available.")
                return

    async def _handle_reminder_callback(self, query, action: str, reminder_id: str) -> None:
        if not action or not reminder_id or not self._reminders:
            await query.edit_message_text("This reminder action is no longer available.")
            return

        now = datetime.now(timezone.utc)
        if action == "done":
            reminder = self._reminders.mark_completed(reminder_id, now=now)
            if reminder and self._memory and reminder.get("task_id"):
                self._memory.complete_task(reminder["task_id"])
            status_text = "Marked done."
        elif action == "later":
            reminder = self._reminders.snooze_reminder(reminder_id, now=now)
            status_text = (
                f"Okay, I’ll remind you again {self._describe_follow_up(reminder)}."
                if reminder is not None
                else ""
            )
        else:
            reminder = None
            status_text = ""

        if reminder is None:
            await query.edit_message_text("This reminder is no longer available.")
            return

        await query.edit_message_text(_render_callback_acknowledgement(reminder, status_text))
        record_activity(
            event="telegram_reminder_callback_handled",
            component="telegram",
            summary="Handled Telegram reminder action without agent invocation",
            metadata={"action": action, "reminder_id": reminder["id"]},
        )

    async def _handle_chat_reset_callback(self, query, action: str, session_id: str) -> None:
        if not action or not session_id or not self._chat_reset:
            await query.edit_message_text("This chat-reset action is no longer available.")
            return

        now = datetime.now(timezone.utc)
        if action == "reset":
            self._agent.reset_history()
            session = self._chat_reset.reset_session(session_id, now=now)
            text = "Chat reset. Context cleared and reminders stopped."
        elif action == "dismiss":
            session = self._chat_reset.dismiss_session(session_id, now=now)
            text = "Chat-reset reminders dismissed for this session."
        else:
            session = None
            text = ""

        if session is None:
            await query.edit_message_text("This chat-reset session is no longer available.")
            return

        await query.edit_message_text(text)
        record_activity(
            event="telegram_chat_reset_callback_handled",
            component="telegram",
            summary="Handled chat-reset reminder action without agent invocation",
            metadata={"action": action, "session_id": session["id"]},
        )

    def _describe_follow_up(self, reminder: dict) -> str:
        next_run_at = str(reminder.get("next_run_at") or "").replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(next_run_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return "later"
        local_dt = dt.astimezone()
        return f"at {local_dt.strftime('%H:%M %Z on %Y-%m-%d')}"

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        doc: Document = update.message.document
        await self._queue_file_to_drive(update, context, doc.file_id, doc.file_name, doc.mime_type)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Use highest resolution photo
        photo: PhotoSize = update.message.photo[-1]
        await self._queue_file_to_drive(update, context, photo.file_id, f"photo_{photo.file_id}.jpg", "image/jpeg")

    async def _queue_file_to_drive(
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

        chat_id = getattr(getattr(update, "effective_chat", None), "id", settings.TELEGRAM_ALLOWED_USER_ID)
        await update.message.reply_text(f"Queued {filename} for filing. I'll send the result once processing finishes.")
        record_activity(
            event="telegram_file_queued",
            component="telegram",
            summary="Queued Telegram file for background processing",
            metadata={"filename": filename, "mime_type": mime_type},
        )

        task = asyncio.create_task(self._file_to_drive(context, chat_id, file_id, filename, mime_type))
        if not hasattr(self, "_background_tasks"):
            self._background_tasks = set()
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _file_to_drive(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        file_id: str,
        filename: str,
        mime_type: str,
    ) -> None:
        op_id = new_op_id("telegram-file")
        with operation_context(op_id):
            record_activity(
                event="telegram_file_received",
                component="telegram",
                summary="Received Telegram file for Drive filing",
                metadata={"filename": filename, "mime_type": mime_type},
            )

            try:
                tg_file = await context.bot.get_file(file_id)
                data = bytes(await tg_file.download_as_bytearray())

                from agent_sdk.filer import classify_attachment, classify_attachment_locally
                from memory.schema import MemoryCategory, MemoryConfidence, MemoryRecord, MemorySource
                from utils.anonymization import prepare_text_for_remote_processing
                from utils.anonymization_store import upsert_anonymized_document
                from utils.text_extraction import extract_text

                text_content = extract_text(data, mime_type, filename)
                model_text, anonymization_result, review_reason = prepare_text_for_remote_processing(
                    text_content,
                    filename=filename,
                    mime_type=mime_type,
                    raw_data=data,
                )
                if review_reason:
                    record_issue(
                        level="WARNING",
                        event="telegram_file_local_classification_fallback",
                        component="telegram",
                        status="warning",
                        summary="Telegram document classified locally because anonymized text was unavailable",
                        metadata={"filename": filename, "mime_type": mime_type, "reason": review_reason},
                    )
                    classification = classify_attachment_locally(
                        filename,
                        mime_type,
                        text_content,
                        summary_reason=review_reason,
                    )
                else:
                    classification = classify_attachment(filename, mime_type, model_text, raw_data=data)

                folder_id = self._drive.get_or_create_folder_path(
                    classification.top_level, classification.sub_folder
                )
                drive_file_id = self._drive.upload_bytes(data, classification.filename, folder_id, mime_type)

                record = MemoryRecord(
                    topic=f"file:{classification.filename}",
                    summary=classification.summary,
                    category=MemoryCategory.DOCUMENT_REF,
                    source=MemorySource.TELEGRAM,
                    confidence=MemoryConfidence.HIGH,
                    document_ref=drive_file_id,
                )
                self._memory.upsert(record)

                if anonymization_result and anonymization_result.sanitized_text.strip():
                    upsert_anonymized_document(
                        drive_file_id=drive_file_id,
                        content_sha256=anonymization_result.content_sha256,
                        original_filename=classification.filename,
                        mime_type=mime_type,
                        sanitized_text=anonymization_result.sanitized_text,
                        backend=anonymization_result.backend,
                        model=anonymization_result.model,
                        replacement_counts=anonymization_result.replacement_counts,
                        truncated=anonymization_result.truncated,
                    )

                if classification.top_level == "Finances" and model_text:
                    from utils.financial_extraction import extract_financial_data
                    financial = extract_financial_data(model_text, classification.filename)
                    if financial:
                        self._memory.add_financial_record(
                            vendor=financial["vendor"],
                            amount=financial["amount"],
                            currency=financial["currency"],
                            category=financial["category"],
                            date=financial["date"],
                            description=classification.summary,
                            drive_file_id=drive_file_id,
                            source="telegram",
                        )

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"Filed to *{classification.top_level}/{classification.sub_folder}/{classification.filename}*\n"
                        f"{classification.summary}"
                    ),
                    parse_mode="Markdown",
                )
                record_activity(
                    event="telegram_file_filed",
                    component="telegram",
                    summary="Filed Telegram document to Drive",
                    metadata={"top_level": classification.top_level, "sub_folder": classification.sub_folder},
                )
                record_audit(
                    event="telegram_file_filed",
                    component="telegram",
                    summary="Filed Telegram document to Drive",
                    metadata={"filename": classification.filename, "top_level": classification.top_level},
                )
            except Exception as e:
                logger.exception("Failed to file document %s", filename)
                record_issue(
                    level="ERROR",
                    event="telegram_file_filing_failed",
                    component="telegram",
                    status="error",
                    summary="Failed to file Telegram document",
                    metadata={"filename": filename, "mime_type": mime_type, "error": str(e)},
                )
                await context.bot.send_message(chat_id=chat_id, text=f"Failed to file document: {e}")


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


def _parse_callback_data(data: str) -> tuple[str, str, str]:
    parts = str(data or "").split(":", 2)
    if len(parts) != 3:
        return "", "", ""
    return parts[0], parts[1], parts[2]


def _render_callback_acknowledgement(reminder: dict, status_text: str) -> str:
    lines = [status_text.strip()] if status_text.strip() else []
    message = str(reminder.get("message") or "").strip()
    if message:
        lines.append(f"Reminder: {message}")
    task_id = str(reminder.get("task_id") or "").strip()
    if task_id:
        lines.append(f"Task: {task_id[:8]}")
    return "\n".join(lines) if lines else "Reminder updated."


def _parse_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _format_success_rate(success_count: int, call_count: int) -> str:
    if call_count <= 0:
        return "0%"
    return f"{(success_count / call_count) * 100:.1f}%"


def _format_cost(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.6f}"


def _total_cost_footer() -> str:
    summary = _load_llmops_summary()
    return f"Total LLM cost so far: {_format_cost(summary['estimated_cost_usd'])}"


def _load_llmops_summary(limit: int = 500) -> dict[str, Any]:
    if not LLM_ACTIVITY_PATH.exists():
        return {
            "call_count": 0,
            "success_count": 0,
            "error_count": 0,
            "avg_latency_ms": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": None,
            "priced_call_count": 0,
            "model_count": 0,
            "last_recorded_at": "",
            "top_tasks": [],
        }

    total_latency_ms = 0.0
    success_count = 0
    error_count = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    total_cost = 0.0
    priced_call_count = 0
    last_recorded_at = ""
    models: set[str] = set()
    tasks: dict[str, dict[str, float | int | str]] = defaultdict(
        lambda: {"task": "", "call_count": 0, "latency_ms": 0.0, "total_tokens": 0}
    )

    try:
        with LLM_ACTIVITY_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-limit:]
    except Exception as exc:
        logger.warning("Failed to read LLM activity file: %s", exc)
        lines = []

    for raw in lines:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        task = str(payload.get("task", "")).strip() or "unknown"
        status = str(payload.get("status", "")).strip() or "unknown"
        model = str(payload.get("model", "")).strip() or "unknown"
        latency_ms = _parse_float(payload.get("latency_ms"))
        current_total_tokens = _parse_int(payload.get("total_tokens"))
        current_input_tokens = _parse_int(payload.get("input_tokens"))
        current_output_tokens = _parse_int(payload.get("output_tokens"))
        estimated_cost = payload.get("estimated_cost_usd")
        estimated_cost_usd = None if estimated_cost in (None, "") else _parse_float(estimated_cost)

        total_latency_ms += latency_ms
        input_tokens += current_input_tokens
        output_tokens += current_output_tokens
        total_tokens += current_total_tokens
        models.add(model)
        if status == "ok":
            success_count += 1
        else:
            error_count += 1
        if estimated_cost_usd is not None:
            total_cost += estimated_cost_usd
            priced_call_count += 1
        recorded_at = str(payload.get("recorded_at", "")).strip()
        if recorded_at:
            last_recorded_at = recorded_at

        task_stats = tasks[task]
        task_stats["task"] = task
        task_stats["call_count"] = int(task_stats["call_count"]) + 1
        task_stats["latency_ms"] = float(task_stats["latency_ms"]) + latency_ms
        task_stats["total_tokens"] = int(task_stats["total_tokens"]) + current_total_tokens

    call_count = sum(int(task["call_count"]) for task in tasks.values())
    top_tasks = sorted(
        (
            {
                "task": str(task["task"]),
                "call_count": int(task["call_count"]),
                "avg_latency_ms": float(task["latency_ms"]) / int(task["call_count"]),
                "total_tokens": int(task["total_tokens"]),
            }
            for task in tasks.values()
            if int(task["call_count"]) > 0
        ),
        key=lambda item: (-item["total_tokens"], item["task"]),
    )[:3]

    return {
        "call_count": call_count,
        "success_count": success_count,
        "error_count": error_count,
        "avg_latency_ms": (total_latency_ms / call_count) if call_count else 0.0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(total_cost, 6) if priced_call_count else None,
        "priced_call_count": priced_call_count,
        "model_count": len(models),
        "last_recorded_at": last_recorded_at,
        "top_tasks": top_tasks,
    }


def _load_ops_health_summary(limit: int = 500) -> dict[str, Any]:
    activity_payloads = read_jsonl(OPS_ACTIVITY_PATH, limit=limit)
    issue_payloads = read_jsonl(OPS_ISSUES_PATH, limit=limit)
    audit_payloads = read_jsonl(OPS_AUDIT_PATH, limit=limit)

    heartbeat_status = "missing"
    heartbeat_age_text = "unknown"
    heartbeat = next(
        (payload for payload in reversed(activity_payloads) if str(payload.get("event", "")) == "app_heartbeat"),
        None,
    )
    if heartbeat is not None:
        recorded_at = str(heartbeat.get("ts", "")).replace("Z", "+00:00")
        try:
            heartbeat_time = datetime.fromisoformat(recorded_at)
            if heartbeat_time.tzinfo is None:
                heartbeat_time = heartbeat_time.replace(tzinfo=timezone.utc)
            age_seconds = max(int((datetime.now(timezone.utc) - heartbeat_time.astimezone(timezone.utc)).total_seconds()), 0)
            heartbeat_status = "running" if age_seconds <= HEARTBEAT_INTERVAL_SECONDS * 2 else "stale"
            heartbeat_age_text = _format_age(age_seconds)
        except ValueError:
            pass

    return {
        "heartbeat_status": heartbeat_status,
        "heartbeat_age_text": heartbeat_age_text,
        "issue_count": len(issue_payloads),
        "audit_count": len(audit_payloads),
    }


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    remainder = seconds % 60
    return f"{minutes}m {remainder}s"
