import json
import os
import tempfile
import unittest

import sessions

TEST_SID = "agent-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TEST_SID_B = "agent-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TEST_SID_A = "agent-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class TestSessionRegistry(unittest.TestCase):
    def test_register_creates_entry_with_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            sessions.register(path, TEST_SID, title="Hello world")
            reg = sessions.load_registry(path)
            self.assertEqual(reg.active_id, TEST_SID)
            self.assertEqual(len(reg.sessions), 1)
            self.assertEqual(reg.sessions[0].title, "Hello world")

    def test_record_exchange_sets_title_once_and_updates_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            sid = TEST_SID
            sessions.register(path, sid, title="New chat")
            sessions.record_exchange(
                path, sid, user_text="Fix the bug", assistant_text="I will fix it."
            )
            reg = sessions.load_registry(path)
            entry = reg.sessions[0]
            self.assertEqual(entry.title, "Fix the bug")
            self.assertIn("fix it", entry.summary.lower())
            sessions.record_exchange(
                path, sid, user_text="Another question", assistant_text="Here is the answer."
            )
            reg = sessions.load_registry(path)
            self.assertEqual(reg.sessions[0].title, "Fix the bug")
            self.assertIn("answer", reg.sessions[0].summary.lower())

    def test_format_chats_list_active_marker_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            reg = sessions.SessionRegistry(
                active_id=TEST_SID_B,
                sessions=[
                    sessions.SessionEntry(
                        id=TEST_SID_A,
                        created_at="2026-07-05T10:00:00",
                        last_active_at="2026-07-05T10:00:00",
                        title="Older chat",
                        summary="Old summary",
                    ),
                    sessions.SessionEntry(
                        id=TEST_SID_B,
                        created_at="2026-07-05T11:00:00",
                        last_active_at="2026-07-05T11:30:00",
                        title="Newer chat",
                        summary="New summary",
                    ),
                ],
            )
            sessions.save_registry(path, reg)
            text = sessions.format_chats_list(path)
            self.assertIn("Your chats (2)", text)
            self.assertIn("(active)", text)
            self.assertIn("Newer chat", text)
            self.assertIn("Older chat", text)
            self.assertLess(text.index("Newer chat"), text.index("Older chat"))

    def test_prune_when_over_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            reg = sessions.SessionRegistry()
            for i in range(sessions.MAX_SESSIONS + 5):
                reg.sessions.append(
                    sessions.SessionEntry(
                        id=f"agent-{i:08d}-0000-0000-0000-000000000000",
                        created_at=f"2026-01-{min(i + 1, 28):02d}T00:00:00",
                        last_active_at=f"2026-01-{min(i + 1, 28):02d}T00:00:00",
                        title=f"Chat {i}",
                    )
                )
            sessions.save_registry(path, reg)
            sessions.register(path, "agent-ffffffff-ffff-ffff-ffff-ffffffffffff", title="Latest")
            reg = sessions.load_registry(path)
            self.assertEqual(len(reg.sessions), sessions.MAX_SESSIONS)
            ids = {s.id for s in reg.sessions}
            self.assertIn("agent-ffffffff-ffff-ffff-ffff-ffffffffffff", ids)
            self.assertNotIn("agent-00000000-0000-0000-0000-000000000000", ids)

    def test_legacy_migration_skips_cli_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_path = os.path.join(tmp, "sessions.json")
            session_path = os.path.join(tmp, ".cursor_agent_session")
            sid = "82158677-e29c-4718-b123-456789abcdef"
            with open(session_path, "w") as f:
                f.write(sid)
            reg = sessions.load_registry(sessions_path, session_file=session_path)
            self.assertIsNone(reg.active_id)
            self.assertEqual(len(reg.sessions), 0)
            self.assertFalse(os.path.isfile(sessions_path))

    def test_load_registry_filters_legacy_cli_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            reg = sessions.SessionRegistry(
                active_id="82158677-e29c-4718-b123-456789abcdef",
                sessions=[
                    sessions.SessionEntry(
                        id="82158677-e29c-4718-b123-456789abcdef",
                        created_at="2026-07-05T10:00:00",
                        last_active_at="2026-07-05T10:00:00",
                        title="Old CLI chat",
                    ),
                    sessions.SessionEntry(
                        id="agent-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        created_at="2026-07-05T11:00:00",
                        last_active_at="2026-07-05T11:00:00",
                        title="SDK chat",
                    ),
                ],
            )
            sessions.save_registry(path, reg)
            loaded = sessions.load_registry(path)
            self.assertEqual(len(loaded.sessions), 1)
            self.assertEqual(loaded.sessions[0].id, "agent-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            self.assertEqual(loaded.active_id, "agent-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    def test_resolve_by_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            reg = sessions.SessionRegistry(
                sessions=[
                    sessions.SessionEntry(
                        id=TEST_SID_A,
                        created_at="2026-07-05T10:00:00",
                        last_active_at="2026-07-05T11:00:00",
                        title="Recent",
                    ),
                    sessions.SessionEntry(
                        id=TEST_SID_B,
                        created_at="2026-07-05T09:00:00",
                        last_active_at="2026-07-05T10:00:00",
                        title="Older",
                    ),
                ]
            )
            sessions.save_registry(path, reg)
            first = sessions.resolve_by_index(path, 1)
            second = sessions.resolve_by_index(path, 2)
            self.assertEqual(first.title, "Recent")
            self.assertEqual(second.title, "Older")
            self.assertIsNone(sessions.resolve_by_index(path, 99))

    def test_format_chats_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            text = sessions.format_chats_list(path)
            self.assertIn("No chats yet", text)

    def test_record_exchange_summarize_does_not_change_title(self):
        from commands import SUMMARIZE_PROMPT

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            sid = TEST_SID
            sessions.register(path, sid, title="Fix the bug")
            sessions.record_exchange(
                path, sid, user_text=SUMMARIZE_PROMPT, assistant_text="Summary bullets."
            )
            reg = sessions.load_registry(path)
            self.assertEqual(reg.sessions[0].title, "Fix the bug")
            self.assertIn("Summary", reg.sessions[0].summary)

    def test_corrupt_registry_does_not_run_legacy_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_path = os.path.join(tmp, "sessions.json")
            session_path = os.path.join(tmp, ".cursor_agent_session")
            sid = "82158677-e29c-4718-b123-456789abcdef"
            with open(session_path, "w") as f:
                f.write(sid)
            with open(sessions_path, "w") as f:
                f.write("{not valid json")
            reg = sessions.load_registry(sessions_path, session_file=session_path)
            self.assertEqual(reg.active_id, None)
            self.assertEqual(len(reg.sessions), 0)
            self.assertTrue(os.path.isfile(sessions_path + ".bak"))


if __name__ == "__main__":
    unittest.main()
