"""The dashboard's Settings tab must serve and change the real settings store.

The voice process re-reads the store at the start of every listening turn, so a
value saved here is what the microphone actually uses next turn — these tests
pin that round trip, the 750 ms end-of-speech default, and the error paths.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp.web as aiohttp_web

from athena import web
from athena.settings.store import RuntimeSettingsStore


class DashboardSettingsTests(unittest.IsolatedAsyncioTestCase):
    def make_state(self) -> SimpleNamespace:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = RuntimeSettingsStore(Path(directory.name) / "settings.json")
        return SimpleNamespace(settings=store)

    async def test_the_catalog_lists_every_setting_with_type_and_range(self):
        state = self.make_state()
        with patch.object(web, "_require_auth", return_value=state):
            response = await web.settings_status(SimpleNamespace())
        payload = json.loads(response.body)
        self.assertIn("vad_end_silence_ms", payload["settings"])
        spec = payload["settings"]["vad_end_silence_ms"]
        self.assertEqual(spec["default"], 750)
        self.assertEqual(spec["value"], 750)
        self.assertEqual(spec["value_type"], "int")
        self.assertEqual(spec["minimum"], 200)
        self.assertTrue(spec["applies_live"])
        # Every catalog entry carries what the UI needs to build an editor.
        for name, entry in payload["settings"].items():
            for key in ("value", "default", "description", "value_type",
                        "minimum", "maximum", "choices", "applies_live"):
                self.assertIn(key, entry, f"{name} is missing {key}")

    async def test_setting_a_value_round_trips_through_the_store(self):
        state = self.make_state()
        request = SimpleNamespace(json=AsyncMock(
            return_value={"name": "vad_end_silence_ms", "value": 800}))
        with patch.object(web, "_require_post", return_value=state):
            response = await web.settings_control(request)
        payload = json.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["value"], 800)
        self.assertTrue(payload["applies_live"])
        self.assertEqual(state.settings.get("vad_end_silence_ms"), 800)

    async def test_an_out_of_range_value_is_rejected(self):
        state = self.make_state()
        request = SimpleNamespace(json=AsyncMock(
            return_value={"name": "vad_end_silence_ms", "value": 20}))
        with patch.object(web, "_require_post", return_value=state):
            with self.assertRaises(aiohttp_web.HTTPBadRequest):
                await web.settings_control(request)

    async def test_an_unknown_setting_is_rejected(self):
        state = self.make_state()
        request = SimpleNamespace(json=AsyncMock(
            return_value={"name": "DASHSCOPE_API_KEY", "value": "x"}))
        with patch.object(web, "_require_post", return_value=state):
            with self.assertRaises(aiohttp_web.HTTPBadRequest):
                await web.settings_control(request)

    async def test_a_missing_name_is_rejected(self):
        state = self.make_state()
        request = SimpleNamespace(json=AsyncMock(return_value={"value": 5}))
        with patch.object(web, "_require_post", return_value=state):
            with self.assertRaises(aiohttp_web.HTTPBadRequest):
                await web.settings_control(request)

    async def test_reset_returns_the_setting_to_its_default(self):
        state = self.make_state()
        state.settings.set("vad_end_silence_ms", 900)
        request = SimpleNamespace(json=AsyncMock(
            return_value={"name": "vad_end_silence_ms", "action": "reset"}))
        with patch.object(web, "_require_post", return_value=state):
            response = await web.settings_control(request)
        payload = json.loads(response.body)
        self.assertEqual(payload["value"], 750)
        self.assertEqual(state.settings.get("vad_end_silence_ms"), 750)


if __name__ == "__main__":
    unittest.main()
