import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeBot:
    def __init__(self):
        self.calls = []

    async def set_my_commands(self, commands, scope=None):
        self.calls.append((commands, scope))


class FakeApplication:
    def __init__(self):
        self.bot = FakeBot()


class FakeMessage:
    def __init__(self):
        self.calls = []
        self.text = "Hello"
        self.actions = []
        self.chat = SimpleNamespace(send_action=self._send_action)

    async def reply_text(self, text, parse_mode=None):
        self.calls.append((text, parse_mode))

    async def _send_action(self, action):
        self.actions.append(action)


class FakeUpdate:
    def __init__(self):
        self.message = FakeMessage()
        self.effective_user = SimpleNamespace(id=12345)
        self.effective_chat = SimpleNamespace(id=12345)


class FakeProactiveBot:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.calls = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.calls.append((chat_id, text, reply_markup))
        if self.should_fail:
            raise RuntimeError("network error")


class FakeManagedProactiveBot:
    instances = []

    def __init__(self, token):
        self.token = token
        self.calls = []
        self.events = []
        type(self).instances.append(self)

    async def __aenter__(self):
        self.events.append("enter")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.events.append("exit")

    async def send_message(self, chat_id, text, reply_markup=None):
        self.calls.append((chat_id, text, reply_markup))


class FakeReminders:
    def list_reminders(self, status):
        return [
            {
                "id": "abcd1234-0000-0000-0000-000000000000",
                "message": "Call doctor",
                "next_run_at": "2026-04-06T08:00:00+00:00",
                "status": status,
                "recurrence": None,
                "task_id": None,
            }
        ]

    def describe_reminder(self, reminder):
        return f"[{reminder['id'][:8]}] {reminder['status']} for 2026-04-06 10:00 CEST (one-off) - {reminder['message']}"


class FakeMemory:
    def __init__(self):
        self.completed = []

    def complete_task(self, task_id):
        self.completed.append(task_id)
        return True


class FakeChatResetManager:
    def __init__(self):
        self.started = []
        self.reset_calls = []
        self.dismiss_calls = []

    def start_session(self, *, now=None, force_new=False):
        self.started.append({"now": now, "force_new": force_new})
        return {"id": "chatreset-1234"}

    def reset_session(self, session_id=None, *, now=None):
        self.reset_calls.append({"session_id": session_id, "now": now})
        return {"id": session_id or "chatreset-1234"}

    def dismiss_session(self, session_id=None, *, now=None):
        self.dismiss_calls.append({"session_id": session_id, "now": now})
        return {"id": session_id or "chatreset-1234"}


class FakeCallbackQuery:
    def __init__(self, data):
        self.data = data
        self.answered = 0
        self.edits = []

    async def answer(self):
        self.answered += 1

    async def edit_message_text(self, text):
        self.edits.append(text)


class FakeCallbackUpdate:
    def __init__(self, data):
        self.callback_query = FakeCallbackQuery(data)


class TelegramBotTests(unittest.TestCase):
    def test_publish_bot_commands_registers_default_and_chat_scope(self):
        module = load_module("tested_telegram_bot", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        app = FakeApplication()

        asyncio.run(bot._publish_bot_commands(app))

        self.assertEqual(len(app.bot.calls), 2)
        command_names = [command.command for command in app.bot.calls[0][0]]
        self.assertEqual(command_names, ["status", "llmops", "memories", "reminders", "forget", "reset", "linkedin"])
        self.assertIsNone(app.bot.calls[0][1])
        self.assertEqual(
            app.bot.calls[1][1].chat_id,
            module.settings.TELEGRAM_ALLOWED_USER_ID,
        )

    def test_reminders_command_lists_scheduled_reminders(self):
        module = load_module("tested_telegram_bot_reminders", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        bot._reminders = FakeReminders()
        update = FakeUpdate()

        asyncio.run(bot._cmd_reminders(update, SimpleNamespace(args=["scheduled"])))

        self.assertEqual(len(update.message.calls), 1)
        text, parse_mode = update.message.calls[0]
        self.assertEqual(parse_mode, "Markdown")
        self.assertIn("*Reminders (scheduled)*", text)
        self.assertIn("Call doctor", text)

    def test_llmops_command_reports_usage_summary(self):
        module = load_module("tested_telegram_bot_llmops", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        update = FakeUpdate()

        with TemporaryDirectory() as td:
            temp_path = Path(td) / "llm_activity.jsonl"
            ops_activity_path = Path(td) / "ops_activity.jsonl"
            ops_issues_path = Path(td) / "ops_issues.jsonl"
            ops_audit_path = Path(td) / "ops_audit.jsonl"
            temp_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "recorded_at": "2026-04-05T15:05:00+00:00",
                                "task": "chat",
                                "model": "anthropic/claude-sonnet-4.6",
                                "status": "ok",
                                "latency_ms": 800.0,
                                "input_tokens": 1000,
                                "output_tokens": 200,
                                "total_tokens": 1200,
                                "estimated_cost_usd": 0.006,
                            }
                        ),
                        json.dumps(
                            {
                                "recorded_at": "2026-04-05T15:06:00+00:00",
                                "task": "relevance",
                                "model": "google/gemma-4-31b-it",
                                "status": "validation_error",
                                "latency_ms": 120.0,
                                "input_tokens": 80,
                                "output_tokens": 30,
                                "total_tokens": 110,
                                "estimated_cost_usd": None,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            module.LLM_ACTIVITY_PATH = temp_path
            module.OPS_ACTIVITY_PATH = ops_activity_path
            module.OPS_ISSUES_PATH = ops_issues_path
            module.OPS_AUDIT_PATH = ops_audit_path
            ops_activity_path.write_text("", encoding="utf-8")
            ops_issues_path.write_text("", encoding="utf-8")
            ops_audit_path.write_text("", encoding="utf-8")

            asyncio.run(bot._cmd_llmops(update, SimpleNamespace()))

        self.assertEqual(len(update.message.calls), 1)
        text, parse_mode = update.message.calls[0]
        self.assertEqual(parse_mode, "Markdown")
        self.assertIn("*LLMOps*", text)
        self.assertIn("Calls: 2", text)
        self.assertIn("Success: 50.0%", text)
        self.assertIn("Tokens: 1080 in / 230 out / 1310 total", text)
        self.assertIn("Estimated cost: $0.006000 (1/2 priced)", text)
        self.assertIn("*Top tasks*", text)
        self.assertIn("- chat: 1 calls, 1200 tokens, 800.0 ms avg", text)

    def test_proactive_notifier_sends_message(self):
        module = load_module("tested_telegram_bot_proactive", "telegram_bot/bot.py")
        fake_bot = FakeProactiveBot()
        notifier = module.TelegramProactiveNotifier(
            enabled=True,
            chat_id=12345,
            bot=fake_bot,
            max_message_length=8,
        )

        with patch.object(module, "record_activity"), patch.object(module, "record_issue"):
            sent = notifier.send_message("Email filed successfully")

        self.assertTrue(sent)
        self.assertEqual(
            fake_bot.calls,
            [
                (12345, "Email fi", None),
                (12345, "led succ", None),
                (12345, "essfully", None),
            ],
        )

    def test_proactive_notifier_honors_disabled_flag(self):
        module = load_module("tested_telegram_bot_proactive_disabled", "telegram_bot/bot.py")
        fake_bot = FakeProactiveBot()
        notifier = module.TelegramProactiveNotifier(enabled=False, chat_id=12345, bot=fake_bot)

        with patch.object(module, "record_activity"), patch.object(module, "record_issue"):
            sent = notifier.send_message("This should not send")

        self.assertFalse(sent)
        self.assertEqual(fake_bot.calls, [])

    def test_proactive_notifier_returns_false_on_error(self):
        module = load_module("tested_telegram_bot_proactive_error", "telegram_bot/bot.py")
        fake_bot = FakeProactiveBot(should_fail=True)
        notifier = module.TelegramProactiveNotifier(enabled=True, chat_id=12345, bot=fake_bot)

        with patch.object(module, "record_activity"), patch.object(module, "record_issue") as issue_mock, patch.object(module.logger, "exception"):
            sent = notifier.send_message("Attempt")

        self.assertFalse(sent)
        self.assertEqual(fake_bot.calls, [(12345, "Attempt", None)])
        issue_mock.assert_called_once()

    def test_proactive_notifier_uses_short_lived_bot_session_when_not_injected(self):
        module = load_module("tested_telegram_bot_proactive_session", "telegram_bot/bot.py")
        FakeManagedProactiveBot.instances = []
        notifier = module.TelegramProactiveNotifier(
            enabled=True,
            bot_token="token-123",
            chat_id=67890,
            max_message_length=32,
        )

        with patch.object(module, "Bot", FakeManagedProactiveBot), patch.object(module, "record_activity"), patch.object(module, "record_issue"):
            sent = notifier.send_message("Short lived session")

        self.assertTrue(sent)
        self.assertEqual(len(FakeManagedProactiveBot.instances), 1)
        instance = FakeManagedProactiveBot.instances[0]
        self.assertEqual(instance.token, "token-123")
        self.assertEqual(instance.events, ["enter", "exit"])
        self.assertEqual(instance.calls, [(67890, "Short lived session", None)])

    def test_handle_message_appends_total_cost_footer(self):
        module = load_module("tested_telegram_bot_message_costs", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        bot._agent = SimpleNamespace(chat=lambda text: "Hello from Marvis")
        bot._chat_reset = None
        update = FakeUpdate()

        with patch.object(
            module,
            "_load_llmops_summary",
            return_value={
                "call_count": 3,
                "success_count": 3,
                "error_count": 0,
                "avg_latency_ms": 100.0,
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "estimated_cost_usd": 0.012345,
                "priced_call_count": 3,
                "model_count": 1,
                "last_recorded_at": "2026-04-09T10:00:00+00:00",
                "top_tasks": [],
            },
        ), patch.object(module, "record_activity"), patch.object(module, "record_issue"), patch.object(module.logger, "info"):
            asyncio.run(bot._handle_message(update, SimpleNamespace()))

        self.assertEqual(update.message.actions, ["typing"])
        self.assertEqual(len(update.message.calls), 1)
        text, parse_mode = update.message.calls[0]
        self.assertIsNone(parse_mode)
        self.assertIn("Hello from Marvis", text)
        self.assertIn("Total LLM cost so far: $0.0123", text)

    def test_handle_message_starts_chat_reset_session_on_first_message(self):
        module = load_module("tested_telegram_bot_chat_reset_start", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        bot._agent = SimpleNamespace(chat=lambda text: "Hello from Marvis", history_is_empty=lambda: True)
        bot._chat_reset = FakeChatResetManager()
        update = FakeUpdate()

        with patch.object(
            module,
            "_load_llmops_summary",
            return_value={
                "call_count": 1,
                "success_count": 1,
                "error_count": 0,
                "avg_latency_ms": 1.0,
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "estimated_cost_usd": 0.0,
                "priced_call_count": 1,
                "model_count": 1,
                "last_recorded_at": "2026-04-09T10:00:00+00:00",
                "top_tasks": [],
            },
        ), patch.object(module, "record_activity"), patch.object(module, "record_issue"), patch.object(module.logger, "info"):
            asyncio.run(bot._handle_message(update, SimpleNamespace()))

        self.assertEqual(len(bot._chat_reset.started), 1)
        self.assertTrue(bot._chat_reset.started[0]["force_new"])

    def test_cmd_reset_stops_chat_reset_session(self):
        module = load_module("tested_telegram_bot_cmd_reset", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        bot._agent = SimpleNamespace(reset_history=lambda: None)
        bot._chat_reset = FakeChatResetManager()
        update = FakeUpdate()

        asyncio.run(bot._cmd_reset(update, SimpleNamespace()))

        self.assertEqual(len(bot._chat_reset.reset_calls), 1)
        self.assertEqual(update.message.calls[0][0], "Conversation history cleared. Long-term memories are intact.")

    def test_handle_callback_query_marks_linked_task_done_without_agent(self):
        module = load_module("tested_telegram_bot_callback_done", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        bot._memory = FakeMemory()
        bot._reminders = SimpleNamespace(
            mark_completed=lambda reminder_id, now=None: {
                "id": reminder_id,
                "message": "Finish taxes",
                "task_id": "task-1234",
            }
        )
        update = FakeCallbackUpdate("reminder:done:abcd1234-0000-0000-0000-000000000000")

        with patch.object(module, "record_activity"):
            asyncio.run(bot._handle_callback_query(update, SimpleNamespace()))

        self.assertEqual(bot._memory.completed, ["task-1234"])
        self.assertEqual(update.callback_query.answered, 1)
        self.assertIn("Marked done.", update.callback_query.edits[0])

    def test_handle_callback_query_snoozes_reminder_without_agent(self):
        module = load_module("tested_telegram_bot_callback_later", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        bot._memory = FakeMemory()
        bot._reminders = SimpleNamespace(
            snooze_reminder=lambda reminder_id, now=None: {
                "id": reminder_id,
                "message": "Finish taxes",
                "task_id": "task-1234",
                "next_run_at": "2026-04-05T10:40:00+00:00",
            }
        )
        update = FakeCallbackUpdate("reminder:later:abcd1234-0000-0000-0000-000000000000")

        with patch.object(module, "record_activity"):
            asyncio.run(bot._handle_callback_query(update, SimpleNamespace()))

        self.assertEqual(update.callback_query.answered, 1)
        self.assertIn("Okay, I’ll remind you again", update.callback_query.edits[0])

    def test_handle_callback_query_resets_chat_without_agent(self):
        module = load_module("tested_telegram_bot_callback_chat_reset", "telegram_bot/bot.py")
        reset_calls = []
        bot = module.TelegramBot.__new__(module.TelegramBot)
        bot._agent = SimpleNamespace(reset_history=lambda: reset_calls.append("reset"))
        bot._chat_reset = FakeChatResetManager()
        bot._reminders = None
        update = FakeCallbackUpdate("chatreset:reset:chatreset-1234")

        with patch.object(module, "record_activity"):
            asyncio.run(bot._handle_callback_query(update, SimpleNamespace()))

        self.assertEqual(reset_calls, ["reset"])
        self.assertEqual(len(bot._chat_reset.reset_calls), 1)
        self.assertIn("Chat reset. Context cleared", update.callback_query.edits[0])

    def test_handle_callback_query_dismisses_chat_reset_without_agent(self):
        module = load_module("tested_telegram_bot_callback_chat_dismiss", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        bot._agent = SimpleNamespace(reset_history=lambda: None)
        bot._chat_reset = FakeChatResetManager()
        bot._reminders = None
        update = FakeCallbackUpdate("chatreset:dismiss:chatreset-1234")

        with patch.object(module, "record_activity"):
            asyncio.run(bot._handle_callback_query(update, SimpleNamespace()))

        self.assertEqual(len(bot._chat_reset.dismiss_calls), 1)
        self.assertIn("dismissed", update.callback_query.edits[0].lower())

    def test_queue_file_to_drive_acknowledges_immediately(self):
        module = load_module("tested_telegram_bot_file_queue", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        bot._drive = object()
        bot._background_tasks = set()
        update = FakeUpdate()
        context = SimpleNamespace(bot=SimpleNamespace())

        async def fake_file_to_drive(_context, _chat_id, _file_id, _filename, _mime_type):
            return None

        bot._file_to_drive = fake_file_to_drive

        asyncio.run(bot._queue_file_to_drive(update, context, "file-1", "scan.pdf", "application/pdf"))

        self.assertEqual(len(update.message.calls), 1)
        self.assertIn("Queued scan.pdf for filing", update.message.calls[0][0])


if __name__ == "__main__":
    unittest.main()
