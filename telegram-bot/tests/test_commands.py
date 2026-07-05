import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from commands import (
    CommandResult,
    format_short_model_list,
    handle_commands,
    parse_models_output,
    parse_telegram_command,
    select_recommended_models,
)


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

    def test_status_shows_full_id(self):
        send = MagicMock()
        sid = "82158677-e29c-4718-b123-456789abcdef"
        result = handle_commands(["/status"], session_id=sid, token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        send.assert_called_once()
        self.assertEqual(send.call_args[0][2], f"Session: {sid}")

    def test_resume_switches_session(self):
        send = MagicMock()
        sid = "82158677-e29c-4718-b123-456789abcdef"
        with tempfile.TemporaryDirectory() as tmp:
            session_file = os.path.join(tmp, ".cursor_agent_session")
            result = handle_commands(
                [f"/resume {sid}"], session_id="other-id", token="t", chat_id=1,
                send_message=send, session_file=session_file, repo_root=tmp,
            )
            self.assertEqual(result.session_id, sid)
            with open(session_file) as f:
                self.assertEqual(f.read(), sid)
        self.assertIn(sid, send.call_args[0][2])

    def test_resume_requires_id(self):
        send = MagicMock()
        result = handle_commands(["/resume"], session_id="abc", token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        self.assertIn("Usage", send.call_args[0][2])

    def test_model_get_default(self):
        send = MagicMock()
        raw = "Available models\n\nauto - Auto (current)\ngpt-5.2 - GPT-5.2\nclaude-opus-4-8-max - Opus 4.8 1M Max"
        with patch("commands.list_available_models", return_value=raw):
            result = handle_commands(
                ["/model"], session_id="abc", token="t", chat_id=1, send_message=send, repo_root=".",
            )
        self.assertTrue(result.handled)
        body = send.call_args[0][2]
        self.assertIn("Current model: Auto", body)
        self.assertIn("Latest per provider:", body)
        self.assertIn("auto — Auto", body)
        self.assertNotIn("Available models", body)

    def test_select_recommended_picks_newest_per_family(self):
        models = parse_models_output(
            "Available models\n\n"
            "auto - Auto\n"
            "gpt-5.4-high - GPT-5.4 High\n"
            "gpt-5.5-high - GPT-5.5 High\n"
            "gpt-5.2-codex-high - Codex 5.2 High\n"
            "gpt-5.3-codex-high - Codex 5.3 High\n"
            "gpt-5.5-high-fast - GPT-5.5 High Fast\n"
            "claude-opus-4-7-max - Opus 4.7 Max\n"
            "claude-opus-4-8-max - Opus 4.8 Max\n"
            "claude-sonnet-5-max - Sonnet 5 Max\n"
            "gemini-3-flash - Gemini Flash\n"
            "gemini-3.1-pro - Gemini Pro\n"
        )
        picked = select_recommended_models(models)
        self.assertIn("auto", picked)
        self.assertIn("gpt-5.5-high", picked)
        self.assertNotIn("gpt-5.4-high", picked)
        self.assertIn("gpt-5.3-codex-high", picked)
        self.assertNotIn("gpt-5.2-codex-high", picked)
        self.assertNotIn("gpt-5.5-high-fast", picked)
        self.assertIn("claude-opus-4-8-max", picked)
        self.assertNotIn("claude-opus-4-7-max", picked)
        self.assertIn("claude-sonnet-5-max", picked)
        self.assertIn("gemini-3.1-pro", picked)
        self.assertIn("gemini-3-flash", picked)

    def test_format_short_model_list(self):
        models = parse_models_output(
            "Available models\n\nauto - Auto\ngpt-5.2 - GPT-5.2\nclaude-opus-4-8-max - Opus Max\n"
            "gpt-5.2-codex-low-fast - Codex Low Fast"
        )
        text = format_short_model_list(models)
        self.assertIn("auto — Auto", text)
        self.assertIn("gpt-5.2 — GPT-5.2", text)
        self.assertNotIn("codex-low-fast", text)
        self.assertIn("/model all", text)

    def test_model_set_persists(self):
        send = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            model_file = os.path.join(tmp, ".cursor_agent_model")
            result = handle_commands(
                ["/model gpt-5.2"], session_id="abc", token="t", chat_id=1,
                send_message=send, model_file=model_file,
            )
            self.assertTrue(result.handled)
            with open(model_file) as f:
                self.assertEqual(f.read(), "gpt-5.2")
        self.assertIn("gpt-5.2", send.call_args[0][2])


    def test_unknown_command_replies_help(self):
        send = MagicMock()
        result = handle_commands(["/foo"], session_id="abc", token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        self.assertIsNone(result.agent_prompt)
        send.assert_called_once()
        self.assertIn("/help", send.call_args[0][2])

    def test_summarize_sets_agent_prompt_when_session_exists(self):
        from commands import SUMMARIZE_PROMPT

        send = MagicMock()
        sid = "82158677-e29c-4718-b123-456789abcdef"
        result = handle_commands(["/summarize"], session_id=sid, token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_prompt, SUMMARIZE_PROMPT)
    def test_summarize_requires_session(self):
        send = MagicMock()
        result = handle_commands(["/summarize"], session_id=None, token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        self.assertIsNone(result.agent_prompt)
        self.assertIn("No active session", send.call_args[0][2])
