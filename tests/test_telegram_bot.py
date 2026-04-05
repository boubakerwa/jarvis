import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path


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


class TelegramBotTests(unittest.TestCase):
    def test_publish_bot_commands_registers_default_and_chat_scope(self):
        module = load_module("tested_telegram_bot", "telegram_bot/bot.py")
        bot = module.TelegramBot.__new__(module.TelegramBot)
        app = FakeApplication()

        asyncio.run(bot._publish_bot_commands(app))

        self.assertEqual(len(app.bot.calls), 2)
        command_names = [command.command for command in app.bot.calls[0][0]]
        self.assertEqual(command_names, ["status", "memories", "forget", "reset"])
        self.assertIsNone(app.bot.calls[0][1])
        self.assertEqual(
            app.bot.calls[1][1].chat_id,
            module.settings.TELEGRAM_ALLOWED_USER_ID,
        )


if __name__ == "__main__":
    unittest.main()
