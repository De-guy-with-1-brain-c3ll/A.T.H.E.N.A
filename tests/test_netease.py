import unittest
from unittest.mock import AsyncMock

from athena.tools.models import ToolResult
from athena.tools.netease import NetEaseMusicTool, NetEasePlayer
from athena.tools.registry import ToolRegistry


class NetEaseToolTests(unittest.IsolatedAsyncioTestCase):
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
