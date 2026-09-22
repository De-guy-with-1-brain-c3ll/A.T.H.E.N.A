"""Local speech through sherpa-onnx: the free backend that replaced the cloud voice.

The model itself is not loaded here. A test suite that needs a 24 MB ONNX file
and a second of CPU to run is a test suite nobody runs, so the model is replaced
with a stand-in that returns a known waveform, and what the tests check is the
part that was actually written: the interface the coordinator drives, the PCM
conversion, the turn sentinel, the family dispatch, and the settings resolution.

The family tests matter more than they look. Choosing the wrong family config for
a model fails with a complaint about ONNX metadata, which points at the model file
rather than at the config — and the two families disagree about the file name, the
sample rate, and whether a voices table exists at all.
"""
import array
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from athena.events import AudioChunk
from athena.tts import (
    SherpaSynthesizer,
    QwenRealtimeSynthesizer,
    build_synthesizer,
    speech_is_billed,
    synthesizer_sample_rate,
    tts_backend,
)
from athena.tts.sherpa import (
    DEFAULT_MODEL,
    _clip,
    _pcm_bytes,
    sherpa_available,
    sherpa_family,
    sherpa_idle_seconds,
    sherpa_model_directory,
    sherpa_model_file,
    sherpa_needs_voices,
    sherpa_sample_rate,
    sherpa_speaker,
    sherpa_speed,
    sherpa_threads,
    split_for_synthesis,
)


class _Settings:
    dashscope_api_key = "test-key"
    tts_model = "qwen3-tts-flash-realtime"
    tts_voice = "Dolce"
    tts_sample_rate = 24_000


class _Audio:
    """The shape sherpa-onnx returns: a plain list of normalised floats."""

    def __init__(self, samples, sample_rate=24_000):
        self.samples = samples
        self.sample_rate = sample_rate


class _FakeTts:
    """Stands in for `sherpa_onnx.OfflineTts`.

    Returns a fixed waveform so the tests can assert on exact byte counts
    instead of on "some audio arrived", which would pass for silence.
    """

    def __init__(self, seconds=0.5, sample_rate=24_000):
        self.seconds = seconds
        self.sample_rate = sample_rate
        self.calls: list[tuple[str, int, float]] = []

    def generate(self, text, sid=0, speed=1.0):
        self.calls.append((text, sid, speed))
        count = int(self.seconds * self.sample_rate)
        # A ramp rather than a constant: a constant would hide a byte-order or
        # quantisation mistake, because every sample would be identical anyway.
        return _Audio([(index % 2000) / 2000.0 for index in range(count)],
                      self.sample_rate)


def make_model_dir(directory: Path, name: str = "kitten-nano-en-v0_8-int8") -> Path:
    """A directory that looks like an installed voice.

    The directory *name* is what selects the model family, so a test that cares
    about the family passes one: a `kitten-` name needs a voices table, a
    `vits-piper-` name must not have one, and getting that wrong is exactly the
    mistake the family detection exists to prevent.
    """
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "model.int8.onnx").write_bytes(b"\x00" * 64)
    (folder / "tokens.txt").write_text("a 1\n", encoding="utf-8")
    if "kitten" in name or "kokoro" in name:
        (folder / "voices.bin").write_bytes(b"\x00" * 64)
    (folder / "espeak-ng-data").mkdir(exist_ok=True)
    return folder


class BackendSelectionTests(unittest.TestCase):
    def test_asking_for_local_speech_without_the_model_falls_back_and_says_so(self):
        with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "sherpa",
                                     "ATHENA_SHERPA_MODEL_DIR": "/nowhere/at/all"}):
            synthesizer = build_synthesizer(_Settings())
        # A silent substitution would be worse than no speech at all.
        self.assertIsInstance(synthesizer, QwenRealtimeSynthesizer)

    def test_asking_for_local_speech_with_the_model_uses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_model_dir(root)
            with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "sherpa",
                                         "ATHENA_SHERPA_MODEL_DIR": str(root),
                                         "ATHENA_SHERPA_MODEL": "kitten-nano-en-v0_8-int8"}):
                synthesizer = build_synthesizer(_Settings())
        self.assertIsInstance(synthesizer, SherpaSynthesizer)

    def test_the_old_kitten_name_still_selects_the_local_voice(self):
        # The first documentation told people to set ATHENA_TTS_BACKEND=kitten.
        # A rename that quietly fell back to the cloud would read as the feature
        # having been removed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_model_dir(root)
            with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "kitten",
                                         "ATHENA_SHERPA_MODEL_DIR": str(root),
                                         "ATHENA_SHERPA_MODEL": "kitten-nano-en-v0_8-int8"}):
                synthesizer = build_synthesizer(_Settings())
        self.assertIsInstance(synthesizer, SherpaSynthesizer)

    def test_the_speaker_rate_follows_the_family(self):
        # VITS Piper voices are 22.05 kHz and Kitten is 24 kHz, and the speaker
        # is opened from this value before the model can report its own — so the
        # default has to follow the family, not be one fixed number.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_model_dir(root)
            with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "sherpa",
                                         "ATHENA_SHERPA_MODEL_DIR": str(root),
                                         "ATHENA_SHERPA_MODEL": "kitten-nano-en-v0_8-int8"}):
                self.assertEqual(synthesizer_sample_rate(_Settings()), 24_000)

    def test_the_vits_rate_is_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_model_dir(root, "vits-piper-en_US-lessac-medium")
            with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "sherpa",
                                         "ATHENA_SHERPA_MODEL_DIR": str(root),
                                         "ATHENA_SHERPA_MODEL": "vits-piper-en_US-lessac-medium"}):
                self.assertEqual(synthesizer_sample_rate(_Settings()), 22_050)

    def test_the_cloud_rate_is_used_when_local_speech_is_not_usable(self):
        # The cloud path takes its rate from settings, which is where the
        # service's own configuration lives — not from an environment variable.
        class _CloudSettings:
            tts_sample_rate = 16_000

        with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "sherpa",
                                     "ATHENA_SHERPA_MODEL_DIR": "/nowhere/at/all"}):
            self.assertEqual(synthesizer_sample_rate(_CloudSettings()), 16_000)

    def test_the_cloud_voice_is_the_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_TTS_BACKEND", None)
            self.assertEqual(tts_backend(), "qwen")

    def test_only_the_cloud_voice_is_billed(self):
        # The coordinator prints a cost estimate after every reply. Printing one
        # for free local speech reads as though the local voice is not really in
        # use — the opposite of what someone switching to it wants to confirm.
        for backend in ("sherpa", "kitten", "vits", "piper"):
            with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": backend}):
                self.assertFalse(speech_is_billed(), backend)
        with patch.dict(os.environ, {"ATHENA_TTS_BACKEND": "qwen"}):
            self.assertTrue(speech_is_billed())


class FamilyDetectionTests(unittest.TestCase):
    def test_a_vits_directory_is_recognised(self):
        self.assertEqual(sherpa_family(Path("/x/vits-piper-en_US-lessac-medium")), "vits")

    def test_a_kitten_directory_is_recognised(self):
        self.assertEqual(sherpa_family(Path("/x/kitten-nano-en-v0_8-int8")), "kitten")

    def test_a_kokoro_directory_is_recognised(self):
        self.assertEqual(sherpa_family(Path("/x/kokoro-int8-en-v0_19")), "kokoro")

    def test_an_unrecognised_name_defaults_to_kitten(self):
        self.assertEqual(sherpa_family(Path("/x/some-voice")), "kitten")

    def test_an_explicit_family_wins(self):
        # For a directory named anything else, the setting is the escape hatch.
        with patch.dict(os.environ, {"ATHENA_SHERPA_FAMILY": "vits"}):
            self.assertEqual(sherpa_family(Path("/x/some-voice")), "vits")

    def test_a_vits_voice_is_found_under_its_own_name(self):
        # A VITS voice is named after the voice, not "model.onnx", so the file
        # has to be found rather than constructed.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "vits-piper-en_US-lessac-medium"
            folder.mkdir(parents=True)
            (folder / "en_US-lessac-medium.onnx").write_bytes(b"\x00" * 64)
            self.assertEqual(sherpa_model_file(folder),
                             folder / "en_US-lessac-medium.onnx")

    def test_a_single_speaker_voice_does_not_need_a_voices_table(self):
        # Requiring voices.bin would refuse a VITS voice that works perfectly.
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_model_dir(Path(tmp), "vits-piper-en_US-lessac-medium")
            self.assertFalse(sherpa_needs_voices(folder))

    def test_a_multi_speaker_voice_does_need_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_model_dir(Path(tmp), "kitten-nano-en-v0_8-int8")
            self.assertTrue(sherpa_needs_voices(folder))


class SettingsTests(unittest.TestCase):
    def test_the_model_is_kept_loaded_by_default(self):
        # Retiring saves 75 MB and costs the 7.6 s reload measured on the board.
        # That is the wrong trade for a voice assistant, where the first reply
        # after a pause is the one someone is standing there waiting for — and
        # retiring is what let a silent turn go unrepaired.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_SHERPA_IDLE_SECONDS", None)
            self.assertEqual(sherpa_idle_seconds(), 0.0)

    def test_a_positive_idle_timeout_is_still_honoured(self):
        with patch.dict(os.environ, {"ATHENA_SHERPA_IDLE_SECONDS": "120"}):
            self.assertEqual(sherpa_idle_seconds(), 120.0)

    def test_the_default_model_directory_is_under_outputs(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_SHERPA_MODEL_DIR", None)
            self.assertEqual(
                sherpa_model_directory(),
                Path("outputs") / "models" / "kitten" / DEFAULT_MODEL)

    def test_pointing_straight_at_a_model_directory_is_accepted(self):
        # A user who sets the variable to the directory holding tokens.txt means
        # that directory, not a directory named after the model inside it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_model_dir(root)
            folder = root / "kitten-nano-en-v0_8-int8"
            with patch.dict(os.environ, {"ATHENA_SHERPA_MODEL_DIR": str(folder)}):
                self.assertEqual(sherpa_model_directory(), folder)

    def test_the_precision_picks_the_model_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_model_dir(Path(tmp))
            with patch.dict(os.environ, {"ATHENA_SHERPA_PRECISION": "int8"}):
                self.assertEqual(sherpa_model_file(folder), folder / "model.int8.onnx")

    def test_float_precision_falls_back_when_there_is_no_full_model(self):
        # The int8 build is what the installer fetches, so asking for fp32 on a
        # machine that only has int8 must still speak rather than fail to load.
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_model_dir(Path(tmp))
            with patch.dict(os.environ, {"ATHENA_SHERPA_PRECISION": "fp32"}):
                self.assertEqual(sherpa_model_file(folder), folder / "model.int8.onnx")

    def test_the_speaker_defaults_to_the_first_voice(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_SHERPA_SPEAKER", None)
            self.assertEqual(sherpa_speaker(), 0)

    def test_a_negative_speaker_is_clamped(self):
        with patch.dict(os.environ, {"ATHENA_SHERPA_SPEAKER": "-3"}):
            self.assertEqual(sherpa_speaker(), 0)

    def test_a_nonsense_speaker_falls_back_instead_of_raising(self):
        with patch.dict(os.environ, {"ATHENA_SHERPA_SPEAKER": "loud"}):
            self.assertEqual(sherpa_speaker(), 0)

    def test_speed_is_clamped_to_what_the_model_accepts(self):
        with patch.dict(os.environ, {"ATHENA_SHERPA_SPEED": "99"}):
            self.assertEqual(sherpa_speed(), 4.0)

    def test_two_threads_is_the_default(self):
        # Four threads measured 2.79x against two threads' 2.70x, which is not
        # worth the two cores capture and playback want.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATHENA_SHERPA_THREADS", None)
            self.assertEqual(sherpa_threads(), 2)

    def test_the_sample_rate_can_be_overridden(self):
        with patch.dict(os.environ, {"ATHENA_SHERPA_SAMPLE_RATE": "22050"}):
            self.assertEqual(sherpa_sample_rate(), 22_050)

    def test_availability_reports_a_missing_model_separately_from_a_missing_import(self):
        with patch.dict(os.environ, {"ATHENA_SHERPA_MODEL_DIR": "/nowhere/at/all"}):
            available, reason = sherpa_available()
        self.assertFalse(available)
        self.assertIn("not there", reason)


class SplitTests(unittest.TestCase):
    def test_short_text_is_one_piece(self):
        self.assertEqual(split_for_synthesis("Alarm set."), ["Alarm set."])

    def test_whitespace_is_collapsed(self):
        self.assertEqual(split_for_synthesis("  Alarm   set.  "), ["Alarm set."])

    def test_empty_text_produces_nothing(self):
        self.assertEqual(split_for_synthesis("   "), [])

    def test_long_text_is_split_at_sentence_ends(self):
        # Splitting mid-clause is audible as an unnatural pause, so the cut is
        # moved back to the nearest sentence end whenever there is one.
        text = "First sentence here. Second sentence here. Third sentence here."
        pieces = split_for_synthesis(text, limit=30)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece) <= 30 for piece in pieces))
        self.assertEqual(" ".join(pieces), text)

    def test_a_clause_longer_than_the_limit_is_still_split(self):
        # A long reply should sound slightly odd, never go missing.
        text = "word " * 40
        pieces = split_for_synthesis(text.strip(), limit=30)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece) <= 30 for piece in pieces))

    def test_nothing_is_lost_in_the_split(self):
        text = "One two three. " * 40
        pieces = split_for_synthesis(text, limit=100)
        self.assertEqual(" ".join(pieces), text.strip())


class PcmConversionTests(unittest.TestCase):
    def test_full_scale_does_not_overflow(self):
        # The hazard being guarded is `array("h", [32768])`, which raises
        # OverflowError rather than wrapping. Scaling by 32767 keeps the whole
        # normalised range representable, at the cost of the most negative
        # sample being one step short of the format's minimum.
        self.assertEqual(_clip(1.0), 32767)
        self.assertEqual(_clip(-1.0), -32767)

    def test_the_conversion_never_overflows_the_container(self):
        import array

        for sample in (-4.0, -1.0, -0.5, 0.0, 0.5, 1.0, 4.0):
            array.array("h", [_clip(sample)])  # must not raise

    def test_out_of_range_samples_are_clamped(self):
        self.assertEqual(_clip(4.0), 32767)
        self.assertEqual(_clip(-4.0), -32767)

    def test_silence_is_silence(self):
        self.assertEqual(_clip(0.0), 0)

    def test_a_byte_view_is_little_endian_16_bit(self):
        values = array.array("h", [1, -1, 32767])
        raw = _pcm_bytes(values)
        self.assertEqual(len(raw), 6)
        self.assertEqual(raw[0:2], b"\x01\x00")

    def test_a_plain_list_is_accepted(self):
        self.assertEqual(_pcm_bytes([0, 0]), b"\x00\x00" * 2)


class _FakeSherpa:
    """The little of sherpa_onnx that the synthesizer touches."""

    def __init__(self, sample_rate=24_000):
        self.sample_rate = sample_rate
        self.built: list = []


class _ReloadingSherpa(SherpaSynthesizer):
    """Loads a stand-in model and counts how many times it did so.

    `connect` is replaced rather than `_load` so a test can watch a retirement be
    repaired without a real ONNX file, which is the only way to reach the
    "no model, no warm-up" state in a unit test.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loads = 0

    async def connect(self) -> None:
        self.loads += 1
        self._loop = asyncio.get_running_loop()
        self._tts = _FakeTts(0.5)


class _FailingSherpa(SherpaSynthesizer):
    """A synthesizer whose model cannot be loaded, however often it tries."""

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        raise RuntimeError("no model here")


class SynthesisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = make_model_dir(Path(self._tmp.name))

    def synthesizer(self, seconds=0.5):
        """A connected synthesizer whose model is a stand-in."""
        synth = SherpaSynthesizer(model_dir=self.folder, idle_seconds=0)
        synth._tts = _FakeTts(seconds)
        synth._loop = asyncio.get_running_loop()
        return synth

    async def collect(self, synth, turn):
        return [chunk async for chunk in synth.audio(turn)]

    async def test_speech_comes_out_for_the_turn(self):
        synth = self.synthesizer()
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Alarm set.")
        await synth.flush(turn)
        chunks = await reader
        self.assertTrue(chunks)
        self.assertTrue(all(chunk.turn_id == turn for chunk in chunks))
        pcm = b"".join(chunk.pcm for chunk in chunks)
        # 0.5s of 24 kHz 16-bit mono.
        self.assertEqual(len(pcm), 24_000)

    async def test_a_retired_model_is_reloaded_rather_than_going_silent(self):
        """The bug this exists for: silence that never repaired itself.

        `_idle_retire` drops the model after a quiet spell, and `warm` clears
        `_warm_task` when it finishes — so afterwards there is no model *and*
        nothing to wait on. `send_text` used to publish the turn sentinel with no
        audio and never start a load, so the first reply after any pause was
        silent and every reply after it was too. It read as the voice having
        broken rather than expired.
        """
        synth = _ReloadingSherpa(model_dir=self.folder, idle_seconds=0)
        synth._tts = _FakeTts(0.5)
        synth._loop = asyncio.get_running_loop()
        # Exactly the state left behind by a retirement: no model, no warm-up.
        synth._tts = None
        synth._warm_task = None

        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Alarm set.")
        await synth.flush(turn)
        chunks = await reader

        self.assertEqual(synth.loads, 1, "the retired model must be loaded again")
        self.assertTrue(chunks, "a retired model must speak, not go silent")

    async def test_a_model_that_cannot_load_still_ends_the_turn(self):
        # The other half of the same path: when the reload genuinely fails the
        # sentinel must still be published, or playback waits forever for audio
        # that is never coming.
        synth = _FailingSherpa(model_dir=self.folder, idle_seconds=0)
        synth._tts = None
        synth._warm_task = None
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Alarm set.")
        await synth.flush(turn)
        self.assertEqual(await asyncio.wait_for(reader, timeout=2), [])

    async def test_a_warm_up_in_flight_is_waited_for_not_duplicated(self):
        # Two loads racing would double the memory and the start-up cost.
        synth = _ReloadingSherpa(model_dir=self.folder, idle_seconds=0)
        synth._tts = None
        synth._loop = asyncio.get_running_loop()
        synth._warm_task = asyncio.create_task(synth.connect())
        await synth._ensure_ready()
        self.assertEqual(synth.loads, 1)

    async def test_the_audio_is_the_format_the_speaker_wants(self):
        # A list of floats handed to the speaker as bytes would be 8-byte
        # doubles read as noise, so the count is the assertion that matters.
        synth = self.synthesizer(seconds=0.25)
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Hi.")
        await synth.flush(turn)
        pcm = b"".join(chunk.pcm for chunk in await reader)
        self.assertEqual(len(pcm), 12_000)
        self.assertEqual(len(pcm) % 2, 0)

    async def test_audio_is_published_in_chunks_not_one_blob(self):
        synth = self.synthesizer(seconds=0.5)
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "A longer sentence for chunking.")
        await synth.flush(turn)
        chunks = await reader
        self.assertGreater(len(chunks), 1)

    async def test_the_turn_ends_so_playback_cannot_hang(self):
        # The sentinel is the whole contract: without it `.audio()` never
        # returns and the reply hangs forever.
        synth = self.synthesizer()
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Done.")
        await synth.flush(turn)
        chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertIsInstance(chunks[0], AudioChunk)

    async def test_an_empty_clause_produces_no_audio(self):
        synth = self.synthesizer()
        turn = uuid4()
        await synth.send_text(turn, "   ")
        self.assertEqual(synth._tts.calls, [])

    async def test_a_second_turn_does_not_reuse_the_first(self):
        synth = self.synthesizer()
        first, second = uuid4(), uuid4()
        reader = asyncio.create_task(self.collect(synth, second))
        await synth.send_text(first, "First reply.")
        # A new turn while the previous was still speaking means it is stale.
        await synth.send_text(second, "Second reply.")
        await synth.flush(second)
        chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertTrue(all(chunk.turn_id == second for chunk in chunks))

    async def test_a_superseded_turn_is_ended_rather_than_left_open(self):
        # `send_text` is awaited, so the first turn's audio is already queued
        # when the second arrives. What matters is not that the audio vanishes
        # but that the first turn is *ended*: a reader waiting on it must not
        # hang, and must not be fed the second turn's audio.
        synth = self.synthesizer()
        first, second = uuid4(), uuid4()
        reader = asyncio.create_task(self.collect(synth, first))
        await synth.send_text(first, "First reply.")
        await synth.send_text(second, "Second reply.")
        chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertTrue(all(chunk.turn_id == first for chunk in chunks))

    async def test_a_superseded_turn_does_not_receive_the_new_turn_s_audio(self):
        synth = self.synthesizer()
        first, second = uuid4(), uuid4()
        reader = asyncio.create_task(self.collect(synth, first))
        await synth.send_text(first, "First reply.")
        await synth.send_text(second, "Second reply.")
        chunks = await asyncio.wait_for(reader, timeout=5)
        # The sentinel published with the supersede closes the first turn, so
        # none of the second turn's audio can leak into it.
        self.assertNotIn(second, [chunk.turn_id for chunk in chunks])

    async def test_speaking_without_connect_ends_the_turn_instead_of_hanging(self):
        synth = SherpaSynthesizer(model_dir=self.folder, idle_seconds=0)
        synth._loop = asyncio.get_running_loop()
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Nobody connected me.")
        await synth.flush(turn)
        chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertEqual(chunks, [])

    async def test_the_speaker_and_speed_reach_the_model(self):
        synth = SherpaSynthesizer(model_dir=self.folder, speaker=3, speed=1.25,
                                  idle_seconds=0)
        synth._tts = _FakeTts()
        synth._loop = asyncio.get_running_loop()
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Hello.")
        await synth.flush(turn)
        await asyncio.wait_for(reader, timeout=5)
        self.assertEqual(synth._tts.calls[0][1], 3)
        self.assertEqual(synth._tts.calls[0][2], 1.25)

    async def test_a_long_reply_is_synthesized_in_pieces(self):
        synth = self.synthesizer(seconds=0.1)
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Sentence one. " * 60)
        await synth.flush(turn)
        await asyncio.wait_for(reader, timeout=5)
        self.assertGreater(len(synth._tts.calls), 1)

    async def test_a_failed_synthesis_still_ends_the_turn(self):
        synth = self.synthesizer()
        synth._tts.generate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "This will fail.")
        await synth.flush(turn)
        chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertEqual(chunks, [])

    async def test_the_work_is_counted(self):
        # The latency figure can round to zero when the stand-in model returns
        # instantly, so what is asserted is the counter that proves the reply
        # went through synthesis at all.
        synth = self.synthesizer()
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Time me.")
        await synth.flush(turn)
        await asyncio.wait_for(reader, timeout=5)
        self.assertEqual(synth.sentences, 1)
        self.assertEqual(len(synth._tts.calls), 1)

    async def test_the_latency_clock_records_real_synthesis(self):
        # A slow model must show up in the figure, because that figure is how
        # the Pi's performance is judged against the cloud voice.
        import time as _time

        class _SlowTts(_FakeTts):
            def generate(self, text, sid=0, speed=1.0):
                _time.sleep(0.02)
                return super().generate(text, sid=sid, speed=speed)

        synth = SherpaSynthesizer(model_dir=self.folder, idle_seconds=0)
        synth._tts = _SlowTts(seconds=0.1)
        synth._loop = asyncio.get_running_loop()
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Time me properly.")
        await synth.flush(turn)
        await asyncio.wait_for(reader, timeout=5)
        # The bound is deliberately well under the 20 ms the fake sleeps:
        # Windows quantises time.monotonic() to the timer tick (~15 ms), and a
        # window measured against that tick can land just below the sleep —
        # 14.9999999 was observed. Half the sleep still separates a real,
        # slow synthesis from a clock that never ran (0.0).
        self.assertGreaterEqual(synth.last_synthesis_ms, 10)

    async def test_cancel_discards_the_turn_s_queued_audio(self):
        # `send_text` is awaited, so by the time it returns its audio is already
        # in the queue. An interruption must therefore *discard* what is queued,
        # not merely mark the turn over — otherwise a superseded reply plays
        # after the interruption, which is very hard to attribute.
        synth = self.synthesizer()
        turn = uuid4()
        await synth.send_text(turn, "Interrupted.")
        self.assertFalse(synth._audio.empty())
        await synth.cancel(turn)
        chunks = await asyncio.wait_for(self.collect(synth, turn), timeout=5)
        self.assertEqual(chunks, [])

    async def test_cancel_keeps_a_different_turn_s_audio(self):
        # Discarding must be per-turn, or cancelling one reply would silently
        # truncate another.
        synth = self.synthesizer()
        first, second = uuid4(), uuid4()
        reader = asyncio.create_task(self.collect(synth, second))
        await synth.send_text(second, "The reply to keep.")
        await synth.cancel(first)
        await synth.flush(second)
        chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertTrue(chunks)
        self.assertTrue(all(chunk.turn_id == second for chunk in chunks))

    async def test_cancelling_mid_synthesis_leaves_no_audio(self):
        # The genuine interruption: a new turn arrives while the previous one is
        # still being synthesized, so the audio is not yet queued.
        synth = self.synthesizer()
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        sender = asyncio.create_task(synth.send_text(turn, "Long enough to interrupt."))
        await asyncio.sleep(0)
        await synth.cancel(turn)
        await sender
        chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertEqual(chunks, [])

    async def test_close_discards_what_is_still_open(self):
        synth = self.synthesizer()
        turn = uuid4()
        await synth.send_text(turn, "Going away.")
        await synth.close()
        chunks = await asyncio.wait_for(self.collect(synth, turn), timeout=5)
        self.assertEqual(chunks, [])
        self.assertIsNone(synth._tts)

    async def test_cache_phrase_returns_pcm_without_playing(self):
        synth = self.synthesizer(seconds=0.5)
        pcm = await synth.cache_phrase("Alarm set.")
        self.assertEqual(len(pcm), 24_000)
        # Nothing was published, so nothing can leak into the next turn.
        self.assertTrue(synth._audio.empty())

    async def test_connect_explains_itself_when_the_model_is_absent(self):
        synth = SherpaSynthesizer(model_dir=Path(self._tmp.name) / "missing")
        with self.assertRaises(RuntimeError) as caught:
            await synth.connect()
        self.assertIn("not there", str(caught.exception))

    async def test_warming_is_optional_and_never_raises(self):
        synth = SherpaSynthesizer(model_dir=Path(self._tmp.name) / "missing")
        self.assertFalse(await synth.warm())

    async def test_a_reply_arriving_during_the_warm_up_is_not_dropped(self):
        # The coordinator starts `warm()` in the background without awaiting it,
        # so the first reply can arrive while the model is still loading. Giving
        # up then would silently drop the first thing ATHENA says, which is the
        # reply a user is most likely to be listening for.
        synth = SherpaSynthesizer(model_dir=self.folder, idle_seconds=0)
        synth._loop = asyncio.get_running_loop()

        class _SlowLoad(SherpaSynthesizer):
            def _load(self):
                import time
                time.sleep(0.05)
                self._tts = _FakeTts(0.25)

        synth._load = _SlowLoad._load.__get__(synth, SherpaSynthesizer)
        warm = asyncio.create_task(synth.warm())
        await asyncio.sleep(0)  # the warm-up has started but not finished

        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "The first thing I say.")
        await synth.flush(turn)
        chunks = await asyncio.wait_for(reader, timeout=5)
        await warm
        self.assertTrue(chunks, "the first reply was dropped while loading")
        pcm = b"".join(chunk.pcm for chunk in chunks)
        self.assertEqual(len(pcm), 12_000)

    async def test_a_failed_warm_up_does_not_hang_the_reply(self):
        # A load that never succeeds must end the turn rather than wait forever.
        synth = SherpaSynthesizer(model_dir=Path(self._tmp.name) / "missing")
        synth._loop = asyncio.get_running_loop()
        self.assertFalse(await synth.warm())
        turn = uuid4()
        reader = asyncio.create_task(self.collect(synth, turn))
        await synth.send_text(turn, "Nobody can speak this.")
        await synth.flush(turn)
        chunks = await asyncio.wait_for(reader, timeout=5)
        self.assertEqual(chunks, [])
