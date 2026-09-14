from array import array
import unittest

from athena.audio.vad import VoiceGate


def pcm_frame(amplitude: int, samples: int = 320) -> bytes:
    return array("h", [amplitude] * samples).tobytes()


class VoiceGateTests(unittest.TestCase):
    def test_exposes_live_audio_diagnostics(self):
        gate = VoiceGate(minimum_rms=500)
        gate.process(pcm_frame(240))
        self.assertEqual(gate.last_rms, 240)
        self.assertGreater(gate.noise_rms, 100)
        self.assertGreaterEqual(gate.threshold, 500)

    def test_ignores_short_noise_burst(self):
        gate = VoiceGate(minimum_rms=500, start_ms=100)
        for _ in range(4):
            self.assertEqual(gate.process(pcm_frame(900)), [])
        self.assertFalse(gate.active)

    def test_opens_after_confirmed_speech_and_preserves_preroll(self):
        gate = VoiceGate(minimum_rms=500, start_ms=100, pre_roll_ms=800)
        gate.process(pcm_frame(50))
        emitted = []
        for _ in range(5):
            emitted = gate.process(pcm_frame(1000))
        self.assertTrue(gate.active)
        self.assertEqual(len(emitted), 6)

    def test_requires_minimum_voiced_duration(self):
        gate = VoiceGate(minimum_rms=500, start_ms=100, minimum_speech_ms=180)
        for _ in range(5):
            gate.process(pcm_frame(1000))
        self.assertFalse(gate.has_enough_speech)
        for _ in range(4):
            gate.process(pcm_frame(1000))
        self.assertTrue(gate.has_enough_speech)

    def test_ends_after_aggressive_silence_window(self):
        gate = VoiceGate(
            minimum_rms=500,
            start_ms=100,
            minimum_speech_ms=180,
            end_silence_ms=220,
        )
        for _ in range(9):
            gate.process(pcm_frame(1000))
        for _ in range(10):
            gate.process(pcm_frame(0))
            self.assertFalse(gate.should_end)
        gate.process(pcm_frame(0))
        self.assertTrue(gate.should_end)

    def test_short_activation_cannot_stick_open_forever(self):
        gate = VoiceGate(
            minimum_rms=500,
            start_ms=100,
            minimum_speech_ms=180,
            end_silence_ms=220,
        )
        for _ in range(5):
            gate.process(pcm_frame(1000))
        self.assertFalse(gate.has_enough_speech)
        for _ in range(11):
            gate.process(pcm_frame(0))
        self.assertTrue(gate.should_end)


if __name__ == "__main__":
    unittest.main()
