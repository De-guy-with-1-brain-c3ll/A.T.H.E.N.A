import unittest
from unittest.mock import AsyncMock

from athena.tools.youtube import YouTubeMusicTool, YouTubePlayer


class YouTubeToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_controls_are_forwarded_to_player(self):
        player = YouTubePlayer(None)
        player.play = AsyncMock()
        player.pause = AsyncMock()
        player.resume = AsyncMock()
        player.next = AsyncMock()
        player.stop = AsyncMock()
        player.queue = [("u", "song")]
        tool = YouTubeMusicTool()
        tool.bind({"youtube_player": player})
        self.assertTrue((await tool.execute({"action": "play", "query": "jazz"})).success)
        self.assertTrue((await tool.execute({"action": "pause"})).success)
        self.assertTrue((await tool.execute({"action": "resume"})).success)
        self.assertTrue((await tool.execute({"action": "next"})).success)
        self.assertTrue((await tool.execute({"action": "stop"})).success)
        player.play.assert_awaited_once_with("jazz")
        player.pause.assert_awaited_once()
        player.resume.assert_awaited_once()
        player.next.assert_awaited_once()
        player.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
