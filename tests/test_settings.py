import tempfile
from pathlib import Path
import unittest

from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry


class RuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_tool_persists_allowlisted_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = RuntimeSettingsStore(path)
            registry = ToolRegistry.discover(services={"settings": store})
            result = await registry.execute(
                "manage_settings",
                {"action": "set", "setting": "vad_minimum_rms", "value": 550},
            )
            self.assertTrue(result.success)
            self.assertTrue(result.data["applies_live"])
            self.assertEqual(RuntimeSettingsStore(path).get("vad_minimum_rms"), 550)

    async def test_rejects_unknown_and_out_of_range_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeSettingsStore(Path(directory) / "settings.json")
            with self.assertRaises(ValueError):
                store.set("DASHSCOPE_API_KEY", "not-allowed")
            with self.assertRaises(ValueError):
                store.set("vad_end_silence_ms", 20)


if __name__ == "__main__":
    unittest.main()
