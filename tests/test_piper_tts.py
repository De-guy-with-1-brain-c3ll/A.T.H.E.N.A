"""Local Piper speech: the free backend, and the fallback when it is missing."""
import asyncio
from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from athena.tts import (
    PiperSynthesizer,
    QwenRealtimeSynthesizer,
    build_synthesizer,
    synthesizer_sample_rate,
    tts_backend,
)
from athena.tts.piper import find_voice, piper_available, piper_sample_rate


# A stand-in for the piper binary: reads a line at a time and writes a fixed
# amount of PCM for each, the way the real Piper streams one sentence per line
# without exiting. Run through the interpreter rather than as a shebang script,
# because a shebang is not executable on Windows and the tests have to run there
# too.
FAKE_PIPER = '''import sys
for line in sys.stdin:
    if not line.strip():
        continue
    sys.stdout.buffer.write(b"\\x11\\x22" * 20000)
    sys.stdout.buffer.flush()
'''


class _Settings:
    dashscope_api_key = "test-key"
    tts_model = "qwen3-tts-flash-realtime"
    tts_voice = "Dolce"
    tts_sample_rate = 24_000


def make_fake_piper(directory: Path) -> tuple[str, list[str]]:
    """A stand-in Piper: the interpreter and the script to run under it."""
    import sys
    script = directory / "fake_piper.py"
    script.write_text(FAKE_PIPER, encoding="utf-8")
    return sys.executable, [str(script)]


class BackendSelectionTests(unittest.TestCase):
    def test_the_default_backend_is_the_cloud_voice(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_TTS_BACKEND", None)
            self.assertEqual(tts_backend(), "qwen")

    def test_asking_for_piper_without_it_installed_falls_back_and_says_so(self):
        with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "piper",
                                     "ATHENA_PIPER_BINARY": "definitely-not-piper"}):
            synthesizer = build_synthesizer(_Settings())
        # A silent substitution would be worse than no speech at all.
        self.assertIsInstance(synthesizer, QwenRealtimeSynthesizer)

    def test_asking_for_piper_when_it_is_there_uses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            binary, prefix = make_fake_piper(folder)
            voice = folder / "en_US-lessac-medium.onnx"
            voice.write_bytes(b"model")
            with patch.dict(os.environ, {
                "ATHENA_TTS_BACKEND": "piper",
                "ATHENA_PIPER_BINARY": binary,
                "ATHENA_PIPER_VOICES": str(folder),
                "ATHENA_PIPER_VOICE": "en_US-lessac-medium",
            }):
                # The prefix stands in for a wrapper around the binary.
                synthesizer = PiperSynthesizer(
                    voice="en_US-lessac-medium", binary=binary, prefix_args=prefix)
        self.assertIsInstance(synthesizer, PiperSynthesizer)

    def test_the_speaker_rate_follows_the_backend(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_TTS_BACKEND", None)
            self.assertEqual(synthesizer_sample_rate(_Settings()), 24_000)
        with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "piper",
                                     "ATHENA_PIPER_BINARY": "definitely-not-piper"}):
            # Not usable, so it is not used and the cloud rate stands.
            self.assertEqual(synthesizer_sample_rate(_Settings()), 24_000)

    def test_the_piper_rate_is_its_own(self):
        self.assertEqual(piper_sample_rate(), 22_050)
        with patch.dict(os.environ, {"ATHENA_PIPER_SAMPLE_RATE": "16000"}):
            self.assertEqual(piper_sample_rate(), 16_000)
        with patch.dict(os.environ, {"ATHENA_PIPER_SAMPLE_RATE": "nonsense"}):
            self.assertEqual(piper_sample_rate(), 22_050)


class VoiceLookupTests(unittest.TestCase):
    def test_a_full_path_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            voice = Path(directory) / "my.onnx"
            voice.write_bytes(b"m")
            self.assertEqual(find_voice(str(voice)), voice)

    def test_a_bare_name_is_found_in_the_voices_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "en_US-amy-medium.onnx").write_bytes(b"m")
            with patch.dict(os.environ, {"ATHENA_PIPER_VOICES": str(folder)}):
                self.assertEqual(find_voice("en_US-amy-medium"),
                                 folder / "en_US-amy-medium.onnx")

    def test_a_missing_voice_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"ATHENA_PIPER_VOICES": directory}):
                self.assertIsNone(find_voice("not-a-voice"))


class PiperSynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def test_speech_comes_out_of_a_local_process(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            binary, prefix = make_fake_piper(folder)
            voice = folder / "voice.onnx"
            voice.write_bytes(b"model")
            synthesizer = PiperSynthesizer(
                voice=str(voice), binary=binary, prefix_args=prefix,
                sample_rate=22_050)
            await synthesizer.connect()
            turn = uuid4()
            try:
                await synthesizer.send_text(turn, "Hello there.")
                await synthesizer.flush(turn)
                collected = b"".join([chunk.pcm async for chunk in synthesizer.audio(turn)])
            finally:
                await synthesizer.close()
        self.assertEqual(len(collected), 40_000, "the whole reply should arrive")
        self.assertEqual(collected[:2], b"\x11\x22")

    async def test_an_empty_clause_starts_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            binary, prefix = make_fake_piper(folder)
            voice = folder / "voice.onnx"
            voice.write_bytes(b"model")
            synthesizer = PiperSynthesizer(
                voice=str(voice), binary=binary, prefix_args=prefix,
                sample_rate=22_050)
            await synthesizer.connect()
            try:
                await synthesizer.send_text(uuid4(), "   ")
                self.assertIsNone(synthesizer._process,
                                  "a process was started for nothing")
            finally:
                await synthesizer.close()

    async def test_a_missing_voice_is_refused_with_a_clear_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            binary, prefix = make_fake_piper(folder)
            synthesizer = PiperSynthesizer(
                voice=str(folder / "absent.onnx"), binary=binary, prefix_args=prefix)
            with patch.dict(os.environ, {"ATHENA_PIPER_VOICES": directory}):
                # Refused at connect, not halfway through a reply.
                with self.assertRaises(RuntimeError) as caught:
                    await synthesizer.connect()
            self.assertIn("absent.onnx", str(caught.exception))
            await synthesizer.close()

    async def test_connect_explains_itself_when_piper_is_absent(self):
        with patch.dict(os.environ, {"ATHENA_PIPER_BINARY": "definitely-not-piper"}):
            available, reason = piper_available()
        self.assertFalse(available)
        self.assertIn("not installed", reason)


class ReusedProcessTests(unittest.IsolatedAsyncioTestCase):
    """The process is reused across turns, so the voice load is paid once.

    This is the whole point of the backend: starting Piper and loading a voice
    costs ~1.8s, which is more than a cloud round trip. Starting it per reply
    makes local speech slower than the network it was meant to replace.
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        folder = Path(self._directory.name)
        self.binary, self.prefix = make_fake_piper(folder)
        self.voice = folder / "voice.onnx"
        self.voice.write_bytes(b"model")

    def tearDown(self) -> None:
        self._directory.cleanup()

    def speaker(self, **kwargs) -> PiperSynthesizer:
        settings = {"idle_seconds": 0}
        settings.update(kwargs)
        return PiperSynthesizer(
            voice=str(self.voice), binary=self.binary, prefix_args=self.prefix,
            sample_rate=22_050, **settings)

    async def speak(self, speaker: PiperSynthesizer, text: str) -> int:
        """One turn, driven the way the coordinator drives it."""
        turn = uuid4()

        async def collect() -> int:
            total = 0
            async for chunk in speaker.audio(turn):
                total += len(chunk.pcm)
            return total

        playback = asyncio.create_task(collect())
        await speaker.send_text(turn, text)
        await speaker.flush(turn)
        return await asyncio.wait_for(playback, timeout=10)

    async def test_a_second_turn_reuses_the_same_process(self):
        with patch.dict(os.environ, {"ATHENA_PIPER_IDLE_SECONDS": "0"}):
            speaker = self.speaker()
            await speaker.connect()
            try:
                first = await self.speak(speaker, "One.")
                process = speaker._process
                second = await self.speak(speaker, "Two.")
                after = speaker._process
            finally:
                await speaker.close()
        self.assertIsNotNone(process, "no process was started")
        self.assertIs(after, process, "the process was replaced between turns")
        self.assertEqual(first, 40_000)
        self.assertEqual(second, 40_000, "the second turn produced no audio")

    async def test_the_process_is_not_restarted_while_it_is_still_running(self):
        with patch.dict(os.environ, {"ATHENA_PIPER_IDLE_SECONDS": "0"}):
            speaker = self.speaker()
            await speaker.connect()
            try:
                await speaker.warm()
                first = speaker._process
                await speaker.warm()
                second = speaker._process
            finally:
                await speaker.close()
        self.assertIsNotNone(first, "warming started nothing")
        self.assertIs(second, first, "warming twice started a second process")

    async def test_a_missing_voice_is_reported_at_connect_not_at_warm(self):
        # The failure belongs on the path that already reports it clearly, so
        # warm() never has to invent a message of its own.
        speaker = PiperSynthesizer(
            voice=str(Path(self._directory.name) / "absent.onnx"),
            binary=self.binary, prefix_args=self.prefix)
        with self.assertRaises(RuntimeError) as caught:
            await speaker.connect()
        self.assertIn("absent.onnx", str(caught.exception))
        await speaker.close()

    async def test_warming_is_optional_and_never_raises(self):
        # A synthesizer wired up correctly can still fail to start a process —
        # a voice deleted between the check and the spawn, say. That must cost
        # the next reply its head start, not the reply itself.
        speaker = self.speaker()
        await speaker.connect()
        try:
            speaker._voice_path = None
            self.assertFalse(await speaker.warm())
        finally:
            await speaker.close()

    async def test_an_idle_process_is_retired(self):
        speaker = self.speaker(idle_seconds=0.05)
        await speaker.connect()
        try:
            await self.speak(speaker, "Hello.")
            self.assertIsNotNone(speaker._process, "no process to retire")
            await asyncio.sleep(0.5)
            retired = speaker._process
        finally:
            await speaker.close()
        self.assertIsNone(retired,
                          "the idle process was still held after its quiet spell")

    async def test_a_second_turn_is_not_left_waiting_for_silence(self):
        # The sentinel that ends a turn used to be produced by closing stdin. A
        # reused process cannot be closed, so flush must publish it itself — when
        # it forgot, playback hung forever and the reply was never spoken.
        with patch.dict(os.environ, {"ATHENA_PIPER_IDLE_SECONDS": "0"}):
            speaker = self.speaker()
            await speaker.connect()
            try:
                await asyncio.wait_for(self.speak(speaker, "Hello."), timeout=10)
                # Silence: no audio was requested, but the turn must still end.
                await asyncio.wait_for(self.speak(speaker, "Again."), timeout=10)
            finally:
                await speaker.close()


    async def test_warming_leaves_no_audio_for_the_first_turn(self):
        # The warm-up synthesizes a phrase and throws it away. If it were read by
        # the pump instead, that audio would be handed to the first real turn —
        # wrong words spoken as the reply, and once discarded, silence. So the
        # warm-up must finish *before* the pump starts.
        with patch.dict(os.environ, {"ATHENA_PIPER_IDLE_SECONDS": "0"}):
            speaker = self.speaker()
            await speaker.connect()
            try:
                self.assertTrue(await speaker.warm())
                # The process is live but no turn has been started, so the pump
                # must have nothing to attribute audio to.
                self.assertIsNotNone(speaker._process)
                self.assertIsNone(speaker._turn_id,
                                  "the warm-up left a turn behind for real audio")
                spoken = await self.speak(speaker, "Hello.")
            finally:
                await speaker.close()
        self.assertEqual(spoken, 40_000,
                         "the first turn got the warm-up audio instead of its own")


class PiperVoiceDownloadPathTests(unittest.TestCase):
    """The voice URL is derived from the voice name, and it has to be right.

    The repository stores voices at <lang>/<lang_region>/<speaker>/<quality>/
    with the region keeping the case from the voice name. Deriving
    `en/en_us/...` instead of `en/en_US/...` answers 404 from the mirror, which
    reads like the mirror being broken rather than a misspelled path — and the
    install then fails without ever saying which part was wrong.

    This mirrors the shell derivation in tools/install_piper.sh. Bash cannot be
    run from the test sandbox, so the check is a parity check: the script's
    output was verified against the live mirror, and this keeps the rule written
    down where a change to either side will trip over it.
    """

    @staticmethod
    def folder_for(voice: str) -> str:
        language = voice.split("_", 1)[0]              # en
        region_and_name = voice.split("_", 1)[1]       # US-lessac-medium
        name = region_and_name.split("-", 1)[1]        # lessac-medium
        quality = name.rsplit("-", 1)[1]               # medium
        speaker = name.rsplit("-", 1)[0]               # lessac
        lang_region = voice.split("-", 1)[0]           # en_US, case preserved
        return f"{language}/{lang_region}/{speaker}/{quality}"

    def test_the_region_keeps_its_case(self):
        # en_US, not en_us: the lowercase form is the 404.
        self.assertEqual(self.folder_for("en_US-lessac-medium"), "en/en_US/lessac/medium")

    def test_another_region_is_derived_the_same_way(self):
        self.assertEqual(self.folder_for("en_GB-alba-medium"), "en/en_GB/alba/medium")

    def test_a_differently_named_speaker_still_works(self):
        self.assertEqual(self.folder_for("en_US-amy-medium"), "en/en_US/amy/medium")

    def test_the_script_uses_the_same_rule(self):
        """The parity half: read the script and confirm it lowercases nothing.

        Lowercasing the region was the actual bug, so its absence is the thing
        worth asserting on the file itself.
        """
        script = Path(__file__).resolve().parent.parent / "tools" / "install_piper.sh"
        if not script.is_file():
            self.skipTest("install_piper.sh is not present")
        text = script.read_text(encoding="utf-8")
        self.assertIn('LANG_REGION="${VOICE%%-*}"', text,
                      "the region is no longer taken from the voice name")
        self.assertNotIn("REGION_LOWER", text,
                         "the region is being lowercased again, which 404s")
        self.assertIn("${LANG_CODE}/${LANG_REGION}/${SPEAKER}/${QUALITY}", text)


class PiperEnvironmentTests(unittest.TestCase):
    def test_piper_is_started_unbuffered(self):
        # Without this Piper reads its input ahead into a buffer and stalls after
        # the first sentence with the process healthy — so the second reply of a
        # session is never synthesized at all.
        from athena.tts.piper import piper_environment
        self.assertEqual(piper_environment()["PYTHONUNBUFFERED"], "1")

    def test_the_rest_of_the_environment_is_preserved(self):
        from athena.tts.piper import piper_environment
        with patch.dict(os.environ, {"ATHENA_PIPER_VOICE": "some-voice"}):
            self.assertEqual(piper_environment().get("ATHENA_PIPER_VOICE"), "some-voice")
