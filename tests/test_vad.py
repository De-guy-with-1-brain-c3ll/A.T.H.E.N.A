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

    # ---- Regressions for the two things that made detection feel flaky ----

    def test_a_single_quiet_frame_during_onset_does_not_lose_the_word(self):
        """Speech starts with quiet consonants; consecutive counting dropped them."""
        gate = VoiceGate(minimum_rms=500, start_ms=100, onset_tolerance_ms=80)
        for _ in range(4):
            self.assertEqual(gate.process(pcm_frame(1000)), [])
        self.assertFalse(gate.active)
        # One dip inside the onset window must not discard the four frames of
        # progress already made.
        gate.process(pcm_frame(50))
        emitted = gate.process(pcm_frame(1000))
        self.assertTrue(gate.active, "a brief dip during onset lost the word")
        self.assertEqual(len(emitted), 6)

    def test_a_long_quiet_stretch_still_abandons_the_onset(self):
        gate = VoiceGate(minimum_rms=500, start_ms=100, onset_tolerance_ms=80)
        for _ in range(4):
            gate.process(pcm_frame(1000))
        for _ in range(6):
            gate.process(pcm_frame(50))
        self.assertFalse(gate.active)
        self.assertEqual(gate._consecutive_voiced, 0)

    def test_a_mid_sentence_dip_does_not_end_the_turn(self):
        """Silence was judged against the threshold that starts a turn."""
        gate = VoiceGate(minimum_rms=500, start_ms=100, end_silence_ms=220)
        for _ in range(9):
            gate.process(pcm_frame(1000))
        self.assertTrue(gate.active)
        # Loud enough to keep speaking, too quiet to have started a turn.
        for _ in range(12):
            gate.process(pcm_frame(400))
            self.assertFalse(gate.should_end, "a quiet moment ended the turn early")
        self.assertEqual(gate.silence_frames, 0)

    def test_holding_the_floor_is_lower_than_claiming_it(self):
        gate = VoiceGate(minimum_rms=500)
        self.assertLess(gate.release_threshold, gate.threshold)

    def test_hearing_speech_is_true_while_a_word_is_starting(self):
        gate = VoiceGate(minimum_rms=500, start_ms=100)
        self.assertFalse(gate.hearing_speech)
        gate.process(pcm_frame(1000))
        self.assertFalse(gate.active)
        self.assertTrue(gate.hearing_speech, "ATHENA would talk over a starting word")

    def test_the_noise_floor_follows_a_changed_room_quickly(self):
        """A fixed learning rate left the gate deaf for minutes after a change."""
        gate = VoiceGate(minimum_rms=2000, noise_multiplier=2.2)
        for _ in range(3):
            gate.process(pcm_frame(900))
        self.assertGreater(gate.noise_rms, 400,
                           "the baseline barely moved towards a much louder room")

    def test_a_quiet_room_keeps_the_floor_low(self):
        gate = VoiceGate(minimum_rms=2000, noise_multiplier=2.2)
        for _ in range(20):
            gate.process(pcm_frame(60))
        self.assertLess(gate.noise_rms, 200)

    def test_configure_can_retune_onset_and_acceptance_live(self):
        gate = VoiceGate(minimum_rms=500, start_ms=100, minimum_speech_ms=180)
        gate.configure(minimum_rms=500, noise_multiplier=2.2, end_silence_ms=220,
                       start_ms=40, minimum_speech_ms=60)
        self.assertEqual(gate.start_frames, 2)
        self.assertEqual(gate.minimum_voiced_frames, 3)


if __name__ == "__main__":
    unittest.main()
