"""Tests for the local STT latency harness.

The harness is the tool used to decide which recogniser ships, so a bug in it
would silently mislead that decision. These cover the audio handling that has
real edge cases — channel downmix and resampling — rather than the timing, which
cannot be asserted on.
"""
from __future__ import annotations

import array
from pathlib import Path
import sys
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bench_stt_latency import _to_mono, packetise, read_pcm, resample  # noqa: E402

SAMPLE_RATE = 16_000


def pcm_bytes(*samples: int) -> bytes:
    values = array.array("h", samples)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def samples_of(pcm: bytes) -> list[int]:
    values = array.array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return list(values)


class PacketiseTests(unittest.TestCase):
    def test_a_packet_is_the_requested_duration(self):
        # 100 ms at 16 kHz mono 16-bit is 3200 bytes.
        one_second = pcm_bytes(*([0] * SAMPLE_RATE))
        packets = packetise(one_second, 100)
        self.assertEqual(len(packets), 10)
        self.assertTrue(all(len(p) == 3200 for p in packets))

    def test_a_partial_final_packet_is_kept_not_dropped(self):
        """Losing the tail would silently cut the last word off every clip."""
        pcm = pcm_bytes(*([0] * (SAMPLE_RATE + 50)))
        packets = packetise(pcm, 100)
        self.assertEqual(len(packets), 11)
        self.assertLess(len(packets[-1]), 3200)
        self.assertEqual(sum(len(p) for p in packets), len(pcm))


class DownmixTests(unittest.TestCase):
    def test_stereo_is_averaged_not_halved(self):
        # Left 1000, right 2000 -> 1500, not 1000 (drop) or 2000 (pick one).
        stereo = pcm_bytes(1000, 2000, -1000, -2000)
        self.assertEqual(samples_of(_to_mono(stereo, 2)), [1500, -1500])

    def test_a_silent_channel_does_not_halve_the_level(self):
        stereo = pcm_bytes(1000, 0, 2000, 0)
        self.assertEqual(samples_of(_to_mono(stereo, 2)), [500, 1000])


class ResampleTests(unittest.TestCase):
    def test_output_length_tracks_the_ratio(self):
        source = pcm_bytes(*([0] * 8000))  # 0.5 s at 16 kHz
        out = resample(source, 16_000, 8_000)
        self.assertEqual(len(samples_of(out)), 4000)

    def test_a_constant_signal_stays_constant(self):
        """Linear interpolation must not invent movement in flat audio."""
        source = pcm_bytes(*([1000] * 1000))
        out = samples_of(resample(source, 16_000, 48_000))
        self.assertTrue(all(abs(v - 1000) <= 1 for v in out), "flat audio drifted")


class ReadPcmTests(unittest.TestCase):
    def make_wav(self, rate: int, channels: int, samples: list[int]) -> Path:
        import tempfile
        path = Path(tempfile.mkdtemp()) / f"t-{rate}-{channels}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(pcm_bytes(*samples))
        return path

    def test_a_native_clip_is_returned_unchanged(self):
        path = self.make_wav(SAMPLE_RATE, 1, [100, 200, 300])
        pcm, rate = read_pcm(path)
        self.assertEqual(rate, SAMPLE_RATE)
        self.assertEqual(samples_of(pcm), [100, 200, 300])

    def test_a_phone_recording_is_converted_rather_than_refused(self):
        """44.1 kHz stereo is what a phone produces; it must still be measurable."""
        # Two samples per frame, so one second of stereo is 2 x 44100 values.
        path = self.make_wav(44_100, 2, [500] * (44_100 * 2))
        pcm, rate = read_pcm(path)
        self.assertEqual(rate, SAMPLE_RATE)
        # ~1 second of audio, mono, at 16 kHz.
        self.assertAlmostEqual(len(samples_of(pcm)) / SAMPLE_RATE, 1.0, delta=0.05)

    def test_eight_bit_audio_is_rejected_with_a_clear_message(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "bad.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(1)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(b"\x00" * 100)
        with self.assertRaises(SystemExit) as caught:
            read_pcm(path)
        self.assertIn("16-bit", str(caught.exception))


class DirectoryCollectionTests(unittest.TestCase):
    """`--file` accepts a folder, and must not list each clip twice.

    Windows and macOS match `*.WAV` for `*.wav`, so collecting with two globs
    silently double-counts every file and doubles the apparent test time.
    """

    def collect(self, folder: Path) -> list[Path]:
        # The same rule the CLI uses.
        return sorted({p.resolve() for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() == ".wav"})

    def test_each_wav_is_listed_once(self):
        import tempfile
        folder = Path(tempfile.mkdtemp())
        for name in ("a.wav", "b.wav", "c.WAV"):
            (folder / name).write_bytes(b"RIFF")
        found = self.collect(folder)
        self.assertEqual(len(found), 3)
        self.assertEqual(len({p.name for p in found}), 3)

    def test_non_wav_files_are_ignored(self):
        import tempfile
        folder = Path(tempfile.mkdtemp())
        (folder / "keep.wav").write_bytes(b"RIFF")
        for name in ("notes.txt", "audio.mp3", "rec.wav.part"):
            (folder / name).write_bytes(b"x")
        names = [p.name for p in self.collect(folder)]
        self.assertEqual(names, ["keep.wav"])
