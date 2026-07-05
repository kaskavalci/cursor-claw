import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from commands import CommandResult, handle_commands, parse_telegram_command


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


class TestHandleCommands(unittest.TestCase):
    def test_help_replies_without_agent(self):
        send = MagicMock()
        result = handle_commands(["/help"], session_id="abc", token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        self.assertIsNone(result.agent_prompt)
        send.assert_called_once()
        self.assertIn("/new", send.call_args[0][2])

    def test_new_creates_session(self):
        send = MagicMock()
        with patch("commands.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="uuid-new-session\n", stderr="")
            with tempfile.TemporaryDirectory() as tmp:
                session_file = os.path.join(tmp, ".cursor_agent_session")
                result = handle_commands(
                    ["/new"], session_id=None, token="t", chat_id=1,
                    send_message=send, session_file=session_file, repo_root=tmp,
                )
                self.assertEqual(result.session_id, "uuid-new-session")
                self.assertTrue(os.path.isfile(session_file))
                with open(session_file) as f:
                    self.assertEqual(f.read(), "uuid-new-session")

    def test_new_with_args_sets_agent_prompt(self):
        send = MagicMock()
        with patch("commands.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="uuid\n", stderr="")
            result = handle_commands(
                ["/new fix it"], session_id="old", token="t", chat_id=1,
                send_message=send, session_file=os.devnull, repo_root=".",
            )
        self.assertEqual(result.agent_prompt, "fix it")

    def test_status_shows_prefix(self):
        send = MagicMock()
        result = handle_commands(["/status"], session_id="abcdef12-rest", token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        send.assert_called_once()
        self.assertIn("abcdef12", send.call_args[0][2])

    def test_summarize_requires_session(self):
        send = MagicMock()
        result = handle_commands(["/summarize"], session_id=None, token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        self.assertIsNone(result.agent_prompt)
        self.assertIn("No active session", send.call_args[0][2])
