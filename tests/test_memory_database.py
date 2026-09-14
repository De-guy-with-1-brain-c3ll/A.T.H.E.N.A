import tempfile
from pathlib import Path
import unittest
from uuid import uuid4

from athena.memory.database import MemoryDatabase, StoredTurn


class MemoryDatabaseTests(unittest.TestCase):
    def test_persists_turn_summary_and_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MemoryDatabase(Path(directory) / "athena.db")
            database.initialize()
            turn_id = uuid4()
            database.save_turn(StoredTurn(turn_id, "My favorite color is blue.", "Noted."))
            database.save_summary("Benjamin is configuring ATHENA.")
            database.upsert_fact("favorite_color", "blue", 0.95, turn_id)

            self.assertEqual(database.recent_turns(1)[0].turn_id, turn_id)
            self.assertEqual(database.get_summary(), "Benjamin is configuring ATHENA.")
            self.assertEqual(database.facts(1)[0], ("favorite_color", "blue", 0.95))
            database.delete_facts(["favorite_color"])
            self.assertEqual(database.facts(1), [])


if __name__ == "__main__":
    unittest.main()
