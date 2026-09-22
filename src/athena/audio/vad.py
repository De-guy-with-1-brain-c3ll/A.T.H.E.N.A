from __future__ import annotations

from array import array
from collections import deque
import sys


class VoiceGate:
    """Adaptive energy gate for 16-bit mono PCM microphone frames.

    Two things made this feel unreliable in practice:

    * Speech onset was lost whenever one quiet frame landed inside the first
      100 ms, because the start counter required *consecutive* voiced frames.
      Real speech begins with quiet consonants and is not that steady, so short
      words were often missed entirely.
    * Silence was measured against the same threshold that starts a turn, so a
      single quiet frame mid-sentence could close the turn and cut the user off.

    Onset now tolerates brief dips, and holding the floor uses a lower release
    threshold than claiming it. That is ordinary hysteresis.
    """

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
        release_ratio: float = 0.6,
        onset_tolerance_ms: int = 80,
    ) -> None:
        self.frame_ms = frame_ms
        self.minimum_rms = minimum_rms
        self.noise_multiplier = noise_multiplier
        self.start_frames = max(1, start_ms // frame_ms)
        self.minimum_voiced_frames = max(1, minimum_speech_ms // frame_ms)
        self.end_silence_frames = max(1, end_silence_ms // frame_ms)
        self.release_ratio = release_ratio
        # How many quiet frames may appear inside the onset window before the
        # attempt is abandoned. Counting only voiced frames keeps the speech
        # requirement honest while still tolerating an unsteady start.
        self.onset_tolerance_frames = max(1, onset_tolerance_ms // frame_ms)
        self._pre_roll: deque[bytes] = deque(maxlen=max(1, pre_roll_ms // frame_ms))
        self._noise_rms = 100.0
        self.last_rms = 0.0
        self.reset()

    def reset(self) -> None:
        self.active = False
        self._consecutive_voiced = 0
        self._onset_gap = 0
        self.voiced_frames = 0
        self.silence_frames = 0
        self._pre_roll.clear()

    def configure(
        self,
        *,
        minimum_rms: int,
        noise_multiplier: float,
        end_silence_ms: int,
        start_ms: int | None = None,
        minimum_speech_ms: int | None = None,
    ) -> None:
        self.minimum_rms = minimum_rms
        self.noise_multiplier = noise_multiplier
        self.end_silence_frames = max(1, end_silence_ms // self.frame_ms)
        if start_ms is not None:
            self.start_frames = max(1, start_ms // self.frame_ms)
        if minimum_speech_ms is not None:
            self.minimum_voiced_frames = max(1, minimum_speech_ms // self.frame_ms)

    @property
    def threshold(self) -> float:
        """Level that starts a turn."""
        return max(float(self.minimum_rms), self._noise_rms * self.noise_multiplier)

    @property
    def release_threshold(self) -> float:
        """Lower level that keeps a turn open, so dips do not end it early."""
        return max(self.threshold * self.release_ratio, float(self.minimum_rms) * self.release_ratio)

    @property
    def noise_rms(self) -> float:
        return self._noise_rms

    @property
    def has_enough_speech(self) -> bool:
        return self.voiced_frames >= self.minimum_voiced_frames

    @property
    def hearing_speech(self) -> bool:
        """True while a turn is open or clearly starting, so ATHENA waits."""
        return self.active or self._consecutive_voiced > 0

    @property
    def should_end(self) -> bool:
        # Always close an activated segment after sustained silence. Requiring
        # the minimum voiced duration here caused short words/noise to leave the
        # gate active forever; acceptance is checked separately afterwards.
        return self.active and self.silence_frames >= self.end_silence_frames

    def _learn(self, rms: float) -> None:
        """Track the room level, moving faster when the estimate is far off.

        A single fixed rate could not follow a room that changed (a fan starting,
        a door closing) within a useful time, which left the gate deaf or
        trigger-happy for minutes afterwards.
        """
        rate = 0.05 if abs(rms - self._noise_rms) < 200 else 0.25
        self._noise_rms = self._noise_rms * (1 - rate) + rms * rate

    def process(self, pcm: bytes) -> list[bytes]:
        """Return frames to send to STT; returns nothing until speech is confirmed."""
        rms = self._rms(pcm)
        self.last_rms = rms
        attack = self.threshold
        voiced = rms >= (self.release_threshold if self.active else attack)

        if self.active:
            if voiced:
                self.voiced_frames += 1
                self.silence_frames = 0
            else:
                self.silence_frames += 1
                # Keep learning the room while a turn is open, so a change of
                # level does not leave a stale baseline behind afterwards.
                self._learn(rms)
            return [pcm]

        self._pre_roll.append(pcm)
        if rms >= attack:
            self._consecutive_voiced += 1
            self._onset_gap = 0
        else:
            # Only learn from frames below the speech threshold, so speech
            # cannot inflate the baseline.
            self._learn(rms)
            if self._consecutive_voiced and self._onset_gap < self.onset_tolerance_frames:
                self._onset_gap += 1
            else:
                self._consecutive_voiced = 0
                self._onset_gap = 0

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
