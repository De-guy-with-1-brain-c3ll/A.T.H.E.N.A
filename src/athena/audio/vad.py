from __future__ import annotations

from array import array
from collections import deque
import sys


class VoiceGate:
    """Lightweight adaptive energy gate for 16-bit mono PCM microphone frames."""

    def __init__(
        self,
        *,
        frame_ms: int = 20,
        minimum_rms: int = 300,
        noise_multiplier: float = 2.2,
        start_ms: int = 100,
        minimum_speech_ms: int = 180,
        end_silence_ms: int = 220,
        pre_roll_ms: int = 800,
    ) -> None:
        self.frame_ms = frame_ms
        self.minimum_rms = minimum_rms
        self.noise_multiplier = noise_multiplier
        self.start_frames = max(1, start_ms // frame_ms)
        self.minimum_voiced_frames = max(1, minimum_speech_ms // frame_ms)
        self.end_silence_frames = max(1, end_silence_ms // frame_ms)
        self._pre_roll: deque[bytes] = deque(maxlen=max(1, pre_roll_ms // frame_ms))
        self._noise_rms = 100.0
        self.last_rms = 0.0
        self.reset()

    def reset(self) -> None:
        self.active = False
        self._consecutive_voiced = 0
        self.voiced_frames = 0
        self.silence_frames = 0
        self._pre_roll.clear()

    def configure(
        self, *, minimum_rms: int, noise_multiplier: float, end_silence_ms: int
    ) -> None:
        self.minimum_rms = minimum_rms
        self.noise_multiplier = noise_multiplier
        self.end_silence_frames = max(1, end_silence_ms // self.frame_ms)

    @property
    def threshold(self) -> float:
        return max(float(self.minimum_rms), self._noise_rms * self.noise_multiplier)

    @property
    def noise_rms(self) -> float:
        return self._noise_rms

    @property
    def has_enough_speech(self) -> bool:
        return self.voiced_frames >= self.minimum_voiced_frames

    @property
    def should_end(self) -> bool:
        # Always close an activated segment after sustained silence. Requiring
        # the minimum voiced duration here caused short words/noise to leave the
        # gate active forever; acceptance is checked separately afterwards.
        return self.active and self.silence_frames >= self.end_silence_frames

    def process(self, pcm: bytes) -> list[bytes]:
        """Return frames to send to STT; returns nothing until speech is confirmed."""
        rms = self._rms(pcm)
        self.last_rms = rms
        voiced = rms >= self.threshold

        if self.active:
            if voiced:
                self.voiced_frames += 1
                self.silence_frames = 0
            else:
                self.silence_frames += 1
            return [pcm]

        self._pre_roll.append(pcm)
        if voiced:
            self._consecutive_voiced += 1
        else:
            self._consecutive_voiced = 0
            # Learn the room level only before activation and only from frames
            # below the speech threshold, so speech cannot inflate the baseline.
            self._noise_rms = self._noise_rms * 0.95 + rms * 0.05

        if self._consecutive_voiced < self.start_frames:
            return []

        self.active = True
        self.voiced_frames = self._consecutive_voiced
        buffered = list(self._pre_roll)
        self._pre_roll.clear()
        return buffered

    @staticmethod
    def _rms(pcm: bytes) -> float:
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return 0.0
        mean_square = sum(sample * sample for sample in samples) / len(samples)
        return mean_square**0.5
