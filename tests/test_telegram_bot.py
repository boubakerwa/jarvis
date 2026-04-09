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

    async def reply_text(self, text, parse_mode=None):
        self.calls.append((text, parse_mode))


class FakeUpdate:
    def __init__(self):
        self.message = FakeMessage()


class FakeProactiveBot:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.calls = []

    async def send_message(self, chat_id, text):
        self.calls.append((chat_id, text))
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

    async def send_message(self, chat_id, text):
        self.calls.append((chat_id, text))


class TelegramBotTests(unittest.TestCase):
    def test_publish_bot_commands_registers_default_and_chat_scope(self):
        module = load_module("tested_telegram_bot", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        app = FakeApplication()

        asyncio.run(bot._publish_bot_commands(app))

        self.assertEqual(len(app.bot.calls), 2)
        command_names = [command.command for command in app.bot.calls[0][0]]
        self.assertEqual(command_names, ["status", "llmops", "memories", "forget", "reset", "linkedin"])
        self.assertIsNone(app.bot.calls[0][1])
        self.assertEqual(
            app.bot.calls[1][1].chat_id,
            module.settings.TELEGRAM_ALLOWED_USER_ID,
        )

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
                (12345, "Email fi"),
                (12345, "led succ"),
                (12345, "essfully"),
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
        self.assertEqual(fake_bot.calls, [(12345, "Attempt")])
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
        self.assertEqual(instance.calls, [(67890, "Short lived session")])


if __name__ == "__main__":
    unittest.main()
