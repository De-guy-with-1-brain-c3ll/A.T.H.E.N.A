import asyncio
import shutil
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

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

    async def test_status_is_answerable_without_a_speaker(self):
        player = YouTubePlayer(None)
        tool = YouTubeMusicTool()
        tool.bind({"youtube_player": player})
        result = await tool.execute({"action": "status"})
        self.assertTrue(result.success)
        self.assertIn("Nothing is playing", result.spoken_text)

    async def test_playing_without_a_query_asks_instead_of_guessing(self):
        tool = YouTubeMusicTool()
        tool.bind({"youtube_player": YouTubePlayer(None)})
        result = await tool.execute({"action": "play"})
        self.assertFalse(result.success)

    async def test_the_spoken_title_survives_an_already_drained_queue(self):
        player = YouTubePlayer(None)
        player.play = AsyncMock()
        player.queue = []
        player.current_title = ""
        tool = YouTubeMusicTool()
        tool.bind({"youtube_player": player})
        result = await tool.execute({"action": "play", "query": "lo-fi beats"})
        self.assertTrue(result.success)
        self.assertIn("lo-fi beats", result.spoken_text)
        self.assertIn("audio only", result.spoken_text)

    async def test_the_resolved_title_is_spoken_when_it_is_known(self):
        player = YouTubePlayer(None)
        player.play = AsyncMock()
        player.queue = []
        player.current_title = "Kyoto Protocol"
        tool = YouTubeMusicTool()
        tool.bind({"youtube_player": player})
        result = await tool.execute({"action": "play", "query": "kyoto protocol"})
        self.assertIn("Kyoto Protocol", result.spoken_text)

    async def test_the_tool_is_unavailable_without_a_bound_player(self):
        result = await YouTubeMusicTool().execute({"action": "play", "query": "x"})
        self.assertFalse(result.success)
        self.assertIn("unavailable", result.spoken_text)

    async def test_it_refuses_rather_than_reporting_a_silent_track(self):
        speaker = NS(available=False)
        player = YouTubePlayer(speaker)
        # The search and download both succeed offline, so without this guard
        # the tool reports success and the audio is dropped by an output that
        # cannot play it.
        player._search = AsyncMock(return_value=[("https://youtu.be/x", "a song")])
        with self.assertRaisesRegex(RuntimeError, "No audio output"):
            await player.play("jazz")

    def test_the_definition_documents_audio_only(self):
        description = YouTubeMusicTool.definition.description.lower()
        self.assertIn("audio only", description)
        for action in ("play", "pause", "resume", "next", "stop", "status"):
            self.assertIn(action, YouTubeMusicTool.definition.parameters["properties"]["action"]["enum"])


class YouTubeStartProvenTests(unittest.IsolatedAsyncioTestCase):
    """`play` must not report success until audio is really moving.

    Resolving a video and launching ffmpeg both succeed on a board that cannot
    actually play anything, so returning as soon as the background task exists
    is what produced "it said it was playing and nothing came out".
    """

    def player(self):
        speaker = NS(available=True, music_format=(24000, 1), play=AsyncMock(),
                     stop=AsyncMock())
        return YouTubePlayer(speaker)

    async def test_play_returns_only_after_audio_reaches_the_speaker(self):
        player = self.player()
        player._search = AsyncMock(return_value=[("https://youtu.be/x", "a song")])
        player._resolve = AsyncMock(return_value="https://cdn.example/stream")

        class Stream:
            def __init__(self): self.sent = False
            async def read(self, _n):
                if self.sent: return b""
                self.sent = True
                return b"\x00\x00" * 480

        async def spawn(*_a, **_k):
            process = NS(stdout=Stream(), returncode=0, wait=AsyncMock(),
                         kill=lambda: None, terminate=lambda: None)
            player.process = process
            return process

        with patch("asyncio.create_subprocess_exec", spawn):
            await player.play("jazz")
        player.speaker.play.assert_awaited()
        self.assertEqual(player.last_error, "")

    async def test_a_resolved_video_that_produces_no_audio_is_a_failure(self):
        player = self.player()
        player._search = AsyncMock(return_value=[("https://youtu.be/x", "a song")])
        player._resolve = AsyncMock(return_value="https://cdn.example/stream")

        async def spawn(*_a, **_k):
            empty = NS(stdout=NS(read=AsyncMock(return_value=b"")), returncode=1,
                       wait=AsyncMock(), kill=lambda: None, terminate=lambda: None)
            player.process = empty
            return empty

        with patch("asyncio.create_subprocess_exec", spawn):
            with self.assertRaisesRegex(RuntimeError, "no playable audio"):
                await player.play("jazz")

    async def test_a_resolve_failure_is_reported_instead_of_hanging(self):
        player = self.player()
        player._search = AsyncMock(return_value=[("https://youtu.be/x", "a song")])
        player._resolve = AsyncMock(side_effect=RuntimeError("yt-dlp is not installed"))
        with self.assertRaisesRegex(RuntimeError, "yt-dlp is not installed"):
            await player.play("jazz")

    async def test_nothing_left_playing_after_a_failed_start(self):
        player = self.player()
        player._search = AsyncMock(return_value=[("https://youtu.be/x", "a song")])
        player._resolve = AsyncMock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            await player.play("jazz")
        self.assertFalse(player.playing)
        self.assertEqual(player.queue, [])


class YouTubeDuckingTests(unittest.IsolatedAsyncioTestCase):
    """YouTube shares one speaker with Athena's voice, so it must yield."""

    def test_it_matches_the_netEase_ducking_contract(self):
        player = YouTubePlayer(None)
        for name in ("suspend_for_voice", "resume_after_voice", "wait_for_voice", "status"):
            self.assertTrue(callable(getattr(player, name, None)), name)

    def test_playback_is_blocked_while_athena_is_talking(self):
        player = YouTubePlayer(None)
        self.assertTrue(player._voice_clear.is_set())
        player.suspend_for_voice()
        self.assertFalse(player._voice_clear.is_set(), "audio would talk over the reply")
        player.resume_after_voice()
        self.assertTrue(player._voice_clear.is_set())

    def test_nested_speech_does_not_resume_too_early(self):
        player = YouTubePlayer(None)
        player.suspend_for_voice()
        player.suspend_for_voice()
        player.resume_after_voice()
        self.assertFalse(player._voice_clear.is_set(),
                         "resumed while a reply was still speaking")
        player.resume_after_voice()
        self.assertTrue(player._voice_clear.is_set())

    async def test_pause_holds_the_stream_and_resume_releases_it(self):
        player = YouTubePlayer(None)
        player.task = asyncio.create_task(asyncio.sleep(10))
        await asyncio.sleep(0)
        try:
            await player.pause()
            self.assertFalse(player._playback_clear.is_set())
            self.assertTrue(player.status()["paused"])
            await player.resume()
            self.assertTrue(player._playback_clear.is_set())
            self.assertFalse(player.status()["paused"])
        finally:
            player.task.cancel()
            await asyncio.gather(player.task, return_exceptions=True)

    async def test_controls_refuse_when_nothing_is_playing(self):
        player = YouTubePlayer(None)
        for action in ("pause", "resume"):
            with self.assertRaises(RuntimeError):
                await getattr(player, action)()

    async def test_the_audio_format_comes_from_the_speaker(self):
        class Speaker:
            @property
            def music_format(self): return (16_000, 1)
        self.assertEqual(YouTubePlayer(Speaker())._music_format(), (16_000, 1))
        self.assertEqual(YouTubePlayer(None)._music_format(), (24_000, 1))

    def test_it_uses_the_interpreter_that_actually_has_yt_dlp(self):
        # The release installs yt-dlp into ATHENA's venv, which is not on the
        # service PATH. Falling back to a bare `python3 -m yt_dlp` would reach
        # the system interpreter, where the module does not exist.
        with patch.object(shutil, "which", return_value=None):
            command = YouTubePlayer._yt()
        self.assertEqual(command, [sys.executable, "-m", "yt_dlp"])

    def test_a_ytdlp_on_the_path_is_preferred(self):
        with patch.object(shutil, "which", return_value="/usr/bin/yt-dlp"):
            self.assertEqual(YouTubePlayer._yt(), ["/usr/bin/yt-dlp"])

    def test_a_pasted_link_is_not_treated_as_a_search(self):
        from athena.tools.youtube import _looks_like_url
        for value in ("https://youtu.be/abc", "youtube.com/watch?v=x", "www.youtube.com/watch?v=x"):
            self.assertTrue(_looks_like_url(value), value)
        for value in ("lo-fi beats", "a talk about robotics"):
            self.assertFalse(_looks_like_url(value), value)


if __name__ == "__main__":
    unittest.main()
