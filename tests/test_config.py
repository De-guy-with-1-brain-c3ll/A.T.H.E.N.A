import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from athena.config import Settings, project_path_from_environment
from athena.settings.store import RuntimeSettingsStore


class ConfigTests(unittest.TestCase):
    def test_relative_database_path_is_project_relative(self):
        with patch.dict(os.environ, {"ATHENA_DATABASE_PATH": "data/shared.db"}, clear=False):
            path = project_path_from_environment("ATHENA_DATABASE_PATH", "data/athena.db")
        project = Path(__file__).resolve().parents[1]
        self.assertEqual(path, project / "data" / "shared.db")

    def test_absolute_database_path_and_model_overrides_are_respected(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "settings.json"
            database = Path(directory) / "memory.db"
            values = {
                "DASHSCOPE_API_KEY": "speech",
                "DEEPSEEK_API_KEY": "language",
                "ATHENA_DATABASE_PATH": str(database),
                "ATHENA_STT_MODEL": "custom-stt",
                "ATHENA_TTS_MODEL": "custom-tts",
                "DEEPSEEK_MODEL": "custom-llm",
            }
            with patch.dict(os.environ, values, clear=False):
                result = Settings.from_environment(RuntimeSettingsStore(settings_file))
        self.assertEqual(result.database_path, database)
        self.assertEqual((result.stt_model, result.tts_model, result.deepseek_model),
                         ("custom-stt", "custom-tts", "custom-llm"))


if __name__ == "__main__":
    unittest.main()
