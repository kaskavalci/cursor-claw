import unittest
from commands import parse_telegram_command


class TestParseTelegramCommand(unittest.TestCase):
    def test_plain_command(self):
        self.assertEqual(parse_telegram_command("/new"), ("new", ""))

    def test_command_with_args(self):
        self.assertEqual(parse_telegram_command("/new fix the bug"), ("new", "fix the bug"))

    def test_bot_suffix_stripped(self):
        self.assertEqual(parse_telegram_command("/help@MyCursorBot"), ("help", ""))

    def test_not_a_command(self):
        self.assertIsNone(parse_telegram_command("hello"))
        self.assertIsNone(parse_telegram_command("/newbot"))  # BotFather, not ours

    def test_case_insensitive_name(self):
        self.assertEqual(parse_telegram_command("/NEW"), ("new", ""))
