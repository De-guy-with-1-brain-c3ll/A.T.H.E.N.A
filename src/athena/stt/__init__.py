"""Speech recognition backends, chosen by ATHENA_STT_BACKEND."""
from __future__ import annotations

import os

from athena.stt.fun_asr import FunAsrRecognizer
from athena.stt.sensevoice import SenseVoiceRecognizer, sensevoice_available

__all__ = [
    "FunAsrRecognizer",
    "SenseVoiceRecognizer",
    "build_recognizer",
    "stt_backend",
]


def stt_backend() -> str:
    return os.environ.get("ATHENA_STT_BACKEND", "qwen").strip().casefold() or "qwen"


def build_recognizer(settings):
    """Return the configured recogniser.

    `qwen` is the cloud service and is billed per second of audio; `sensevoice`
    runs on this machine and costs nothing. If local recognition is asked for but
    is not usable, the cloud recogniser is returned instead of leaving ATHENA
    unable to hear, and the reason is printed so it is not a silent substitution
    — the same arrangement the synthesizer factory uses.
    """
    if stt_backend() == "sensevoice":
        available, reason = sensevoice_available()
        if available:
            return SenseVoiceRecognizer(settings.stt_sample_rate)
        print(f"Local speech recognition was requested but is not usable ({reason}); "
              "using the cloud recogniser.", flush=True)
    return FunAsrRecognizer(
        settings.dashscope_api_key,
        settings.stt_model,
        settings.stt_sample_rate,
        settings.stt_language,
        prewarm=settings.stt_prewarm,
        max_sentence_silence_ms=settings.stt_max_sentence_silence_ms,
        semantic_punctuation=settings.stt_semantic_punctuation,
    )
