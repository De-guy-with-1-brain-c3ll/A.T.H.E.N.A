"""Saved playlists: choosing one for the hour, and managing them from the dashboard.

The auto-choose is deliberately deterministic — the clock biases mood tags the
user wrote themselves and nothing else — so it can be pinned here and reported
back in a sentence instead of being an unexplainable choice.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from athena.coordinator import VoiceCoordinator
from athena.tools.netease import NetEasePlayer, hour_label, preferred_moods
from athena.voice_ipc import MUSIC_COMMANDS


def library(directory: str) -> NetEasePlayer:
    player = NetEasePlayer(None, Path(directory) / "playlists.json")
    player.add_playlist("Deep Focus", "lofi instrumental beats", ["focus", "study"])
    player.add_playlist("Workout", "high energy electronic", ["exercise", "energy"])
    player.add_playlist("Night Calm", "ambient piano sleep", ["calm", "sleep", "quiet"])
    player.play = AsyncMock()
    return player


class MusicDecodeFormatTests(unittest.IsolatedAsyncioTestCase):
    """Music has to be decoded to what the output device actually wants.

    The board's own PortAudio stream is mono, a browser takes full-rate stereo,
    and the decoder used to be hardcoded to mono 24 kHz for both. Getting this
    wrong is audible, not subtle: the wrong rate changes the pitch.
    """

    class Speaker:
        def __init__(self, rate, channels):
            self.music_format = (rate, channels)
            self.played = []

        async def play(self, pcm, rate=None, channels=None):
            self.played.append((len(pcm), rate, channels))

    class SpeakerWithoutFormat:
        """A speaker from before this existed still has to work."""

        def __init__(self):
            self.played = []

        async def play(self, pcm):
            self.played.append(len(pcm))

    def _process(self, chunk):
        process = MagicMock()
        process.stdout.read = AsyncMock(side_effect=[chunk, b""])
        process.wait = AsyncMock()
        process.communicate = AsyncMock(return_value=(b"", b""))
        process.returncode = 0
        return process

    async def _run(self, speaker, chunk=b"\x00\x00" * 10):
        with tempfile.TemporaryDirectory() as directory:
            player = NetEasePlayer(speaker, Path(directory) / "playlists.json")
            player.queue = [("1", "A Track")]
            player._stream_url = AsyncMock(return_value="http://example/a.mp3")
            process = self._process(chunk)
            spawn = AsyncMock(return_value=process)
            with patch("athena.tools.netease.asyncio.create_subprocess_exec", new=spawn):
                await player._run_queue()
            return spawn, process

    async def test_a_browser_gets_48khz_stereo(self):
        speaker = self.Speaker(48_000, 2)
        spawn, process = await self._run(speaker)
        arguments = list(spawn.await_args.args)
        self.assertEqual(arguments[arguments.index("-ar") + 1], "48000")
        self.assertEqual(arguments[arguments.index("-ac") + 1], "2")
        self.assertEqual(speaker.played, [(20, 48_000, 2)])

    async def test_the_board_speaker_still_gets_mono_24khz(self):
        speaker = self.Speaker(24_000, 1)
        spawn, _ = await self._run(speaker)
        arguments = list(spawn.await_args.args)
        self.assertEqual(arguments[arguments.index("-ar") + 1], "24000")
        self.assertEqual(arguments[arguments.index("-ac") + 1], "1")
        self.assertEqual(speaker.played, [(20, 24_000, 1)])

    async def test_a_packet_is_a_tenth_of_a_second_at_any_format(self):
        speaker = self.Speaker(48_000, 2)
        _, process = await self._run(speaker)
        # 48 kHz * 2 channels * 2 bytes * 0.1 s
        self.assertEqual(process.stdout.read.await_args.args[0], 19_200)

    async def test_cover_art_is_not_sent_to_a_raw_pcm_pipe(self):
        speaker = self.Speaker(48_000, 2)
        spawn, _ = await self._run(speaker)
        self.assertIn("-vn", list(spawn.await_args.args))

    async def test_a_speaker_with_no_format_still_plays(self):
        speaker = self.SpeakerWithoutFormat()
        await self._run(speaker)
        self.assertEqual(speaker.played, [20])

    async def test_the_stream_is_asked_for_at_the_configured_level(self):
        with tempfile.TemporaryDirectory() as directory:
            player = NetEasePlayer(None, Path(directory) / "playlists.json")
            self.assertEqual(player.level, "exhigh")


class PlaylistInventoryTests(unittest.TestCase):
    def test_playlists_serialize_for_the_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            player = library(directory)
            self.assertEqual(
                [entry["name"] for entry in player.playlists()],
                ["Deep Focus", "Workout", "Night Calm"])
            self.assertEqual(player.playlists()[1]["moods"], ["exercise", "energy"])

    def test_no_playlists_is_an_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            player = NetEasePlayer(None, Path(directory) / "playlists.json")
            self.assertEqual(player.playlists(), [])


class TimeOfDayChoiceTests(unittest.TestCase):
    def test_late_night_favours_the_calm_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            player = library(directory)
            name, _ = player.choose_playlist("choose a playlist", hour=23)
            self.assertEqual(name, "Night Calm")

    def test_midday_favours_the_focus_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            player = library(directory)
            name, _ = player.choose_playlist("choose a playlist", hour=13)
            self.assertEqual(name, "Deep Focus")

    def test_an_explicit_mood_still_beats_the_hour(self):
        # Asking for energy at midnight must not be talked out of it by the clock.
        with tempfile.TemporaryDirectory() as directory:
            player = library(directory)
            name, _ = player.choose_playlist("something energetic for my workout", hour=23)
            self.assertEqual(name, "Workout")

    def test_the_reason_says_which_rule_chose(self):
        with tempfile.TemporaryDirectory() as directory:
            player = library(directory)
            player.choose_playlist("choose a playlist", hour=23)
            self.assertEqual(player.last_choice_reason, "it suits the late night")
            player.choose_playlist("something for my workout", hour=13)
            self.assertEqual(player.last_choice_reason, "it matched what you asked for")

    def test_the_clock_maps_to_moods_and_labels(self):
        self.assertIn("calm", preferred_moods(2))
        self.assertIn("focus", preferred_moods(14))
        self.assertIn("energy", preferred_moods(8))
        self.assertEqual(preferred_moods(2), preferred_moods(23))
        self.assertEqual(hour_label(23), "late night")
        self.assertEqual(hour_label(13), "afternoon")


class CoordinatorMusicControlTests(unittest.IsolatedAsyncioTestCase):
    def coordinator(self, player) -> VoiceCoordinator:
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        tool = MagicMock()
        tool.player = player
        coordinator.llm = MagicMock()
        coordinator.llm._tools = {"netease_music": tool}
        return coordinator

    async def test_status_carries_the_playlists(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.coordinator(library(directory))
            state = await coordinator.control_music("status")
        self.assertIn("playlists", state)
        self.assertEqual(len(state["playlists"]), 3)

    async def test_a_playlist_can_be_added_and_removed_from_the_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            player = library(directory)
            coordinator = self.coordinator(player)
            added = await coordinator.control_music(
                "add_playlist", name="Rainy Day", query="rain sounds", moods=["calm", "rain"])
            self.assertIn("Rainy Day", [entry["name"] for entry in added["playlists"]])
            self.assertEqual(added["message"], "Saved the Rainy Day playlist.")
            removed = await coordinator.control_music("remove_playlist", name="Rainy Day")
            self.assertNotIn("Rainy Day", [entry["name"] for entry in removed["playlists"]])

    async def test_playing_a_named_playlist_uses_its_query(self):
        with tempfile.TemporaryDirectory() as directory:
            player = library(directory)
            coordinator = self.coordinator(player)
            state = await coordinator.control_music("play_playlist", name="Workout")
        player.play.assert_awaited_once_with("high energy electronic")
        self.assertEqual(state["message"], "Playing the Workout playlist.")

    async def test_auto_play_chooses_from_the_saved_playlists(self):
        with tempfile.TemporaryDirectory() as directory:
            player = library(directory)
            coordinator = self.coordinator(player)
            state = await coordinator.control_music("auto_play", query="something calm for sleeping")
        played = player.play.await_args.args[0]
        self.assertIn(played, {"lofi instrumental beats", "high energy electronic", "ambient piano sleep"})
        self.assertIn("Playing the", state["message"])

    async def test_an_unknown_action_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self.coordinator(library(directory))
            with self.assertRaises(RuntimeError):
                await coordinator.control_music("set_fire_to_the_speaker")

    async def test_the_socket_allows_exactly_the_playlist_actions(self):
        for action in ("add_playlist", "remove_playlist", "play_playlist", "auto_play"):
            self.assertIn(action, MUSIC_COMMANDS)
        self.assertNotIn("add_track", MUSIC_COMMANDS)


if __name__ == "__main__":
    unittest.main()
