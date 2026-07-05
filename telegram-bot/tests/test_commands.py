import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from commands import (
    CommandResult,
    fetch_available_models,
    format_full_model_list,
    format_short_model_list,
    handle_commands,
    parse_models_output,
    parse_telegram_command,
    select_recommended_models,
)

SDK_SESSION_A = "agent-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SDK_SESSION_B = "agent-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


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
        mock_pool = MagicMock()
        mock_pool.create_session.return_value = SDK_SESSION_A
        with tempfile.TemporaryDirectory() as tmp:
            session_file = os.path.join(tmp, ".cursor_agent_session")
            sessions_file = os.path.join(tmp, "sessions.json")
            result = handle_commands(
                ["/new"], session_id=None, token="t", chat_id=1,
                send_message=send, session_file=session_file,
                sessions_file=sessions_file, repo_root=tmp,
                agent_pool=mock_pool,
            )
            self.assertEqual(result.session_id, SDK_SESSION_A)
            self.assertTrue(os.path.isfile(session_file))
            with open(session_file) as f:
                self.assertEqual(f.read(), SDK_SESSION_A)
            self.assertTrue(os.path.isfile(sessions_file))

    def test_new_with_args_sets_agent_prompt(self):
        send = MagicMock()
        mock_pool = MagicMock()
        mock_pool.create_session.return_value = SDK_SESSION_A
        result = handle_commands(
            ["/new fix it"], session_id="old", token="t", chat_id=1,
            send_message=send, session_file=os.devnull, repo_root=".",
            agent_pool=mock_pool,
        )
        self.assertEqual(result.agent_prompt, "fix it")

    def test_status_shows_full_id(self):
        send = MagicMock()
        sid = SDK_SESSION_A
        result = handle_commands(["/status"], session_id=sid, token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        send.assert_called_once()
        self.assertEqual(send.call_args[0][2], f"Session: {sid}")

    def test_resume_switches_session(self):
        send = MagicMock()
        sid = SDK_SESSION_A
        with tempfile.TemporaryDirectory() as tmp:
            session_file = os.path.join(tmp, ".cursor_agent_session")
            sessions_file = os.path.join(tmp, "sessions.json")
            result = handle_commands(
                [f"/resume {sid}"], session_id="other-id", token="t", chat_id=1,
                send_message=send, session_file=session_file,
                sessions_file=sessions_file, repo_root=tmp,
            )
            self.assertEqual(result.session_id, sid)
            with open(session_file) as f:
                self.assertEqual(f.read(), sid)
        self.assertIn("Resumed", send.call_args[0][2])

    def test_resume_requires_id(self):
        send = MagicMock()
        result = handle_commands(["/resume"], session_id="abc", token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        self.assertIn("Usage", send.call_args[0][2])

    def test_model_get_default(self):
        send = MagicMock()
        models = {
            "auto": "Auto",
            "gpt-5.2": "GPT-5.2",
            "claude-opus-4-8-max": "Opus 4.8 1M Max",
        }
        with patch("commands.fetch_available_models", return_value=models):
            result = handle_commands(
                ["/model"], session_id="abc", token="t", chat_id=1, send_message=send, repo_root=".",
            )
        self.assertTrue(result.handled)
        body = send.call_args[0][2]
        self.assertIn("Current model: auto", body)
        self.assertIn("Latest per provider:", body)
        self.assertIn("auto — Auto", body)
        self.assertNotIn("Available models", body)

    def test_fetch_available_models_uses_sdk(self):
        from cursor_sdk.types import SDKModel

        mock_models = [
            SDKModel(id="auto", display_name="Auto"),
            SDKModel(id="gpt-5.2", display_name="GPT-5.2"),
        ]
        with patch("commands.Cursor") as mock_cursor:
            mock_cursor.models.list.return_value = mock_models
            result = fetch_available_models(api_key="test-key")
        mock_cursor.models.list.assert_called_once_with(api_key="test-key")
        self.assertEqual(result, {"auto": "Auto", "gpt-5.2": "GPT-5.2"})

    def test_format_full_model_list_marks_current(self):
        models = {"auto": "Auto", "gpt-5.2": "GPT-5.2"}
        text = format_full_model_list(models, current_model="auto")
        self.assertIn("auto - Auto (current)", text)
        self.assertIn("gpt-5.2 - GPT-5.2", text)
        self.assertNotIn("gpt-5.2 - GPT-5.2 (current)", text)

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
        sid = SDK_SESSION_A
        result = handle_commands(["/summarize"], session_id=sid, token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_prompt, SUMMARIZE_PROMPT)

    def test_summarize_requires_session(self):
        send = MagicMock()
        result = handle_commands(["/summarize"], session_id=None, token="t", chat_id=1, send_message=send)
        self.assertTrue(result.handled)
        self.assertIsNone(result.agent_prompt)
        self.assertIn("No active session", send.call_args[0][2])

    def test_chats_lists_sessions(self):
        send = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            sessions_file = os.path.join(tmp, "sessions.json")
            import sessions as sessions_mod

            reg = sessions_mod.SessionRegistry(
                active_id=SDK_SESSION_B,
                sessions=[
                    sessions_mod.SessionEntry(
                        id=SDK_SESSION_A,
                        created_at="2026-07-05T10:00:00",
                        last_active_at="2026-07-05T10:00:00",
                        title="First task",
                        summary="Did the first thing.",
                    ),
                    sessions_mod.SessionEntry(
                        id=SDK_SESSION_B,
                        created_at="2026-07-05T11:00:00",
                        last_active_at="2026-07-05T11:00:00",
                        title="Second task",
                        summary="Did the second thing.",
                    ),
                ],
            )
            sessions_mod.save_registry(sessions_file, reg)
            result = handle_commands(
                ["/chats"], session_id=SDK_SESSION_B,
                token="t", chat_id=1, send_message=send, sessions_file=sessions_file,
            )
        self.assertTrue(result.handled)
        body = send.call_args[0][2]
        self.assertIn("Your chats (2)", body)
        self.assertIn("First task", body)
        self.assertIn("Second task", body)
        self.assertIn("(active)", body)

    def test_chats_empty(self):
        send = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            sessions_file = os.path.join(tmp, "sessions.json")
            result = handle_commands(
                ["/chats"], session_id=None, token="t", chat_id=1,
                send_message=send, sessions_file=sessions_file,
            )
        self.assertTrue(result.handled)
        self.assertIn("No chats yet", send.call_args[0][2])

    def test_resume_by_index(self):
        send = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            session_file = os.path.join(tmp, ".cursor_agent_session")
            sessions_file = os.path.join(tmp, "sessions.json")
            import sessions as sessions_mod

            reg = sessions_mod.SessionRegistry(
                active_id=SDK_SESSION_B,
                sessions=[
                    sessions_mod.SessionEntry(
                        id=SDK_SESSION_A,
                        created_at="2026-07-05T11:00:00",
                        last_active_at="2026-07-05T11:00:00",
                        title="Recent",
                    ),
                    sessions_mod.SessionEntry(
                        id=SDK_SESSION_B,
                        created_at="2026-07-05T10:00:00",
                        last_active_at="2026-07-05T10:00:00",
                        title="Older",
                    ),
                ],
            )
            sessions_mod.save_registry(sessions_file, reg)
            result = handle_commands(
                ["/resume 2"], session_id=SDK_SESSION_B,
                token="t", chat_id=1, send_message=send,
                session_file=session_file, sessions_file=sessions_file,
            )
            self.assertEqual(result.session_id, SDK_SESSION_B)
            with open(session_file) as f:
                self.assertEqual(f.read(), SDK_SESSION_B)
        self.assertIn("Older", send.call_args[0][2])
