import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import (
    AgentRunResult,
    SESSION_EXPIRED_MSG,
    run_agent_streaming,
    _assistant_text_from_message,
    _split_for_telegram,
    _parse_segments,
    _RUN_LOCK,
)


class _FakeBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeContent:
    def __init__(self, blocks):
        self.content = blocks


class _FakeAssistantMsg:
    type = "assistant"

    def __init__(self, text: str):
        self.message = _FakeContent([_FakeBlock(text)])


class _FakeRun:
    def __init__(self, messages, final_text="", delay=0):
        self._messages = messages
        self._final = final_text
        self._delay = delay
        self.cancelled = False

    def messages(self):
        if self._delay:
            import time
            end = time.time() + self._delay
            while time.time() < end and not self.cancelled:
                time.sleep(0.05)
        if not self.cancelled:
            yield from self._messages

    def text(self):
        return self._final

    def cancel(self):
        self.cancelled = True


class TestAgentRunner(unittest.TestCase):
    def test_assistant_text_extraction(self):
        msg = _FakeAssistantMsg("Hello world")
        self.assertEqual(_assistant_text_from_message(msg), "Hello world")
        self.assertEqual(_assistant_text_from_message(MagicMock(type="thinking")), "")

    def test_streams_assistant_to_telegram(self):
        send = MagicMock()
        pool = MagicMock()
        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-dddddddd-dddd-dddd-dddd-dddddddddddd"
        pool.get.return_value = mock_agent
        mock_agent.send.return_value = _FakeRun(
            [_FakeAssistantMsg("First reply.\n\nMore text here.")],
            final_text="First reply.\n\nMore text here.",
        )
        with patch("agent_runner.config_loader.get_agent_timeout", return_value=0):
            result = run_agent_streaming(
                pool,
                "ping",
                "agent-dddddddd-dddd-dddd-dddd-dddddddddddd",
                "token",
                1,
                model="auto",
                send_message=send,
                send_chat_action=MagicMock(),
                collapse_blank_lines=lambda t: t,
                send_pending_attachments=MagicMock(),
                send_pending_images=MagicMock(),
                logs_dir=tempfile_dir(),
            )
        self.assertIsInstance(result, AgentRunResult)
        self.assertEqual(send.call_count, 1)
        self.assertIn("First reply", result.assistant_text)
        self.assertIn("More text", result.assistant_text)

    def test_fragmented_stream_sends_one_message(self):
        """Many SDK assistant deltas must be merged into one Telegram message."""
        send = MagicMock()
        pool = MagicMock()
        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-ffffffff-ffff-ffff-ffff-ffffffffffff"
        pool.get.return_value = mock_agent
        deltas = [
            _FakeAssistantMsg("| Col | Val |\n"),
            _FakeAssistantMsg("|-----|-----|\n"),
            _FakeAssistantMsg("| a   | 1   |\n"),
            _FakeAssistantMsg("Some trailing text."),
        ]
        mock_agent.send.return_value = _FakeRun(deltas, final_text="")
        with patch("agent_runner.config_loader.get_agent_timeout", return_value=0):
            result = run_agent_streaming(
                pool,
                "ping",
                "agent-ffffffff-ffff-ffff-ffff-ffffffffffff",
                "token",
                1,
                model="auto",
                send_message=send,
                send_chat_action=MagicMock(),
                collapse_blank_lines=lambda t: t,
                send_pending_attachments=MagicMock(),
                send_pending_images=MagicMock(),
                logs_dir=tempfile_dir(),
            )
        self.assertEqual(send.call_count, 1)
        body = send.call_args[0][2]
        self.assertIn("| Col | Val |", body)
        self.assertIn("| a   | 1   |", body)
        self.assertIn("Some trailing text.", body)
        self.assertIn("| a   | 1   |", result.assistant_text)

    def test_split_preserves_markdown_table(self):
        table = "| Col | Val |\n|-----|-----|\n| a   | 1   |"
        intro = "Intro paragraph.\n\n"
        text = intro + table + "\n\nOutro paragraph."
        segments = _parse_segments(text)
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[1], table)
        chunks = _split_for_telegram(text, max_size=len(intro) + len(table) + 5)
        self.assertEqual(len(chunks), 2)
        self.assertIn("| Col | Val |", chunks[0])
        self.assertIn("| a   | 1   |", chunks[0])
        self.assertIn("Outro", chunks[1])

    def test_empty_stream_fallback(self):
        send = MagicMock()
        pool = MagicMock()
        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        pool.get.return_value = mock_agent
        mock_agent.send.return_value = _FakeRun([], final_text="")
        with patch("agent_runner.config_loader.get_agent_timeout", return_value=0):
            result = run_agent_streaming(
                pool,
                "ping",
                "agent-eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "token",
                1,
                model="auto",
                send_message=send,
                send_chat_action=MagicMock(),
                collapse_blank_lines=lambda t: t,
                send_pending_attachments=MagicMock(),
                send_pending_images=MagicMock(),
                logs_dir=tempfile_dir(),
            )
        self.assertIn("no reply", result.assistant_text.lower())
        bodies = [c[0][2] for c in send.call_args_list if len(c[0]) > 2]
        self.assertTrue(any("no reply" in (b or "").lower() for b in bodies))

    def test_stale_session_auto_recovers_and_notifies_user(self):
        send = MagicMock()
        pool = MagicMock()
        stale_id = "agent-stale-stale-stale-stale-stalestalest"
        new_agent = MagicMock()
        new_agent.agent_id = "agent-fresh-fresh-fresh-fresh-freshfreshfr"
        not_found = _not_found_error()

        def set_active_side_effect(sid):
            if sid == stale_id:
                raise not_found
            return new_agent

        pool.set_active.side_effect = set_active_side_effect
        pool.create_session.return_value = new_agent.agent_id
        pool.get.return_value = new_agent
        new_agent.send.return_value = _FakeRun(
            [_FakeAssistantMsg("Recovered reply.")],
            final_text="Recovered reply.",
        )

        with patch("agent_runner.config_loader.get_agent_timeout", return_value=0):
            result = run_agent_streaming(
                pool,
                "hello after stale session",
                stale_id,
                "token",
                1,
                model="auto",
                send_message=send,
                send_chat_action=MagicMock(),
                collapse_blank_lines=lambda t: t,
                send_pending_attachments=MagicMock(),
                send_pending_images=MagicMock(),
                logs_dir=tempfile_dir(),
            )

        pool.drop_session.assert_called_once_with(stale_id)
        pool.create_session.assert_called_once_with(model="auto")
        notify_calls = [
            c for c in send.call_args_list if SESSION_EXPIRED_MSG in (c[0][2] if len(c[0]) > 2 else "")
        ]
        self.assertEqual(len(notify_calls), 1)
        self.assertEqual(result.session_id, new_agent.agent_id)
        self.assertIn("Recovered", result.assistant_text)

    def test_timeout_cancels_run(self):
        send = MagicMock()
        pool = MagicMock()
        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-timeout-timeout-timeout-timeout-tim"
        pool.get.return_value = mock_agent
        slow_run = _FakeRun([], final_text="", delay=5)
        mock_agent.send.return_value = slow_run
        with patch("agent_runner.config_loader.get_agent_timeout", return_value=1):
            with patch("agent_runner.time.sleep", side_effect=lambda s: None):
                run_agent_streaming(
                    pool,
                    "slow",
                    mock_agent.agent_id,
                    "token",
                    1,
                    model="auto",
                    send_message=send,
                    send_chat_action=MagicMock(),
                    collapse_blank_lines=lambda t: t,
                    send_pending_attachments=MagicMock(),
                    send_pending_images=MagicMock(),
                    logs_dir=tempfile_dir(),
                )
        self.assertTrue(slow_run.cancelled)
        bodies = [c[0][2] for c in send.call_args_list if len(c[0]) > 2]
        self.assertTrue(any("timed out" in (b or "").lower() for b in bodies))
        self.assertFalse(any("no reply" in (b or "").lower() for b in bodies))

    def test_rejects_overlapping_run(self):
        send = MagicMock()
        pool = MagicMock()
        _RUN_LOCK.acquire()
        try:
            result = run_agent_streaming(
                pool,
                "ping",
                None,
                "token",
                1,
                model="auto",
                send_message=send,
                send_chat_action=MagicMock(),
                collapse_blank_lines=lambda t: t,
                send_pending_attachments=MagicMock(),
                send_pending_images=MagicMock(),
                logs_dir=tempfile_dir(),
            )
        finally:
            _RUN_LOCK.release()
        self.assertEqual(result.assistant_text, "")
        send.assert_called_once()
        self.assertIn("busy", send.call_args[0][2].lower())


def _not_found_error():
    from cursor_sdk.errors import AgentNotFoundError

    return AgentNotFoundError("not_found: Agent agent-stale not found")


def tempfile_dir():
    return tempfile.mkdtemp()


if __name__ == "__main__":
    unittest.main()
