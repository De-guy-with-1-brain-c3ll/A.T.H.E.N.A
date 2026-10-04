import unittest
from unittest.mock import AsyncMock
from pathlib import Path
import tempfile

from athena.tools.models import ToolResult
from athena.tools.netease import NetEaseMusicTool, NetEasePlayer
from athena.tools.registry import ToolRegistry


class NetEaseToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_playlists_are_persisted_and_auto_selection_prefers_mood(self):
        with tempfile.TemporaryDirectory() as directory:
            player = NetEasePlayer(None, Path(directory) / "playlists.json")
            player.add_playlist("Focus", "lofi instrumental", ["study", "focus"])
            player.add_playlist("Workout", "high energy electronic", ["exercise", "energy"])
            name, entry = player.choose_playlist("I need focus music while studying")
            self.assertEqual(name, "Focus")
            self.assertEqual(entry["query"], "lofi instrumental")
            loaded = NetEasePlayer(None, Path(directory) / "playlists.json")
            self.assertIn("Focus", loaded.playlist_summary())

    async def test_auto_play_is_routed_without_an_artist_query(self):
        player = NetEasePlayer(None)
        player.choose_playlist = lambda request: ("Focus", {"query": "lofi", "moods": []})
        player.play = AsyncMock()
        tool = NetEaseMusicTool(); tool.bind({"netease_player": player})
        registry = ToolRegistry(); registry.register(tool)
        result = await registry.handle_user_command("choose a playlist for studying")
        self.assertTrue(result.success)
        player.play.assert_awaited_once_with("lofi")

    async def test_plain_play_phrase_bypasses_model(self):
        player = NetEasePlayer(None)
        player.play = AsyncMock()
        player.current_title = "Thunderstruck - AC/DC"
        tool = NetEaseMusicTool()
        tool.bind({"netease_player": player})
        registry = ToolRegistry()
        registry.register(tool)

        result = await registry.handle_user_command("play AC DC Thunderstruck")

        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        player.play.assert_awaited_once_with("ac dc thunderstruck")

    async def test_video_phrase_is_not_hijacked(self):
        player = NetEasePlayer(None)
        player.play = AsyncMock(return_value=ToolResult(True, "ok"))
        tool = NetEaseMusicTool()
        tool.bind({"netease_player": player})
        registry = ToolRegistry()
        registry.register(tool)

        self.assertIsNone(await registry.handle_user_command("play a video about robots"))
        player.play.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
