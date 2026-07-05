import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Tests run from telegram-bot/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_pool import AgentPool, MAX_POOL_SIZE


class TestAgentPool(unittest.TestCase):
    def test_create_session_returns_agent_id(self):
        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-11111111-1111-1111-1111-111111111111"
        with patch("agent_pool.Agent.create", return_value=mock_agent):
            pool = AgentPool("/tmp/repo", "auto")
            sid = pool.create_session()
        self.assertEqual(sid, "agent-11111111-1111-1111-1111-111111111111")
        self.assertEqual(pool.active_id, sid)
        mock_agent.close.assert_not_called()

    def test_get_resumes_when_not_cached(self):
        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        with patch("agent_pool.Agent.resume", return_value=mock_agent) as resume:
            pool = AgentPool("/tmp/repo", "auto")
            agent = pool.get("agent-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        resume.assert_called_once_with("agent-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertIs(agent, mock_agent)

    def test_get_uses_cache_on_second_call(self):
        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        with patch("agent_pool.Agent.resume", return_value=mock_agent) as resume:
            pool = AgentPool("/tmp/repo", "auto")
            pool.get("agent-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
            pool.get("agent-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        resume.assert_called_once()

    def test_eviction_closes_oldest_non_active(self):
        agents = []
        for i in range(MAX_POOL_SIZE + 2):
            a = MagicMock()
            a.agent_id = f"agent-{i:08d}-0000-0000-0000-000000000000"
            agents.append(a)

        with patch("agent_pool.Agent.create", side_effect=agents):
            with patch("agent_pool.Agent.resume") as resume:
                pool = AgentPool("/tmp/repo", "auto")
                for i in range(MAX_POOL_SIZE + 2):
                    pool.create_session()
                pool._active_id = agents[-1].agent_id
                pool._evict_if_needed()
        closed = [a for a in agents if a.close.called]
        self.assertGreaterEqual(len(closed), 1)
        resume.assert_not_called()

    def test_close_all(self):
        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-cccccccc-cccc-cccc-cccc-cccccccccccc"
        with patch("agent_pool.Agent.create", return_value=mock_agent):
            pool = AgentPool("/tmp/repo", "auto")
            pool.create_session()
            pool.close_all()
        mock_agent.close.assert_called_once()
        self.assertEqual(len(pool._agents), 0)

    def test_warm_returns_false_on_agent_not_found(self):
        stale_id = "agent-stale-stale-stale-stale-stalestalest"
        with patch("agent_pool.Agent.resume", side_effect=_not_found_error()):
            pool = AgentPool("/tmp/repo", "auto")
            pool._active_id = stale_id
            ok = pool.warm(stale_id)
        self.assertFalse(ok)
        self.assertIsNone(pool.active_id)
        self.assertNotIn(stale_id, pool._agents)

    def test_set_active_does_not_set_active_id_on_resume_failure(self):
        stale_id = "agent-dead-dead-dead-dead-deaddeaddead"
        with patch("agent_pool.Agent.resume", side_effect=_not_found_error()):
            pool = AgentPool("/tmp/repo", "auto")
            with self.assertRaises(Exception):
                pool.set_active(stale_id)
        self.assertIsNone(pool.active_id)

    def test_drop_session_clears_active_and_cache(self):
        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-drop-drop-drop-drop-dropdropdrop"
        with patch("agent_pool.Agent.create", return_value=mock_agent):
            pool = AgentPool("/tmp/repo", "auto")
            pool.create_session()
            pool.drop_session(mock_agent.agent_id)
        mock_agent.close.assert_called_once()
        self.assertIsNone(pool.active_id)
        self.assertEqual(len(pool._agents), 0)


def _not_found_error():
    from cursor_sdk.errors import AgentNotFoundError

    return AgentNotFoundError("not_found: Agent agent-stale not found")


if __name__ == "__main__":
    unittest.main()
