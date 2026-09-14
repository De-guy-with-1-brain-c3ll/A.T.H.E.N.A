import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from athena.memory.database import MemoryDatabase, StoredTurn
from athena.prompts import packaged_prompt_path, read_prompt, write_prompt
from athena.web import DashboardState, _is_local
from uuid import uuid4


class WebDashboardTests(unittest.TestCase):
    def test_dashboard_accepts_only_local_addresses(self):
        self.assertTrue(_is_local("127.0.0.1"))
        self.assertTrue(_is_local("192.168.31.10"))
        self.assertFalse(_is_local("8.8.8.8"))
        self.assertFalse(_is_local(None))

    def test_signed_session_expires_and_csrf_is_bound_to_it(self):
        state = DashboardState.__new__(DashboardState)
        state.secret = b"a sufficiently long dashboard test secret"
        token = state.issue_session()
        self.assertTrue(state.valid_session(token))
        self.assertFalse(state.valid_session(token + "changed"))
        self.assertNotEqual(state.csrf(token), state.csrf(token + "changed"))

    def test_prompt_override_is_persistent_and_packaged_default_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"ATHENA_DATA_DIR": directory}, clear=False):
                self.assertEqual(read_prompt("system"),
                                 packaged_prompt_path("system").read_text(encoding="utf-8").strip())
                target = write_prompt("system", "Be concise and precise.")
                self.assertEqual(target, Path(directory) / "prompts" / "system_prompt.txt")
                self.assertEqual(read_prompt("system"), "Be concise and precise.")
                with self.assertRaises(ValueError):
                    write_prompt("system", "")

    def test_recent_conversations_include_time_and_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MemoryDatabase(Path(directory) / "athena.db")
            database.initialize()
            first, second = uuid4(), uuid4()
            database.save_turn(StoredTurn(first, "one", "first"))
            database.save_turn(StoredTurn(second, "two", "second"))
            rows = database.recent_conversations()
            self.assertEqual(rows[0].turn_id, second)
            self.assertTrue(rows[0].started_at)


if __name__ == "__main__":
    unittest.main()
