"""Speech synthesis backends, chosen by ATHENA_TTS_BACKEND."""
from __future__ import annotations

import os

from athena.tts.edge import (
    EdgeSynthesizer,
    edge_available,
    edge_sample_rate,
)
from athena.tts.piper import (
    PiperSynthesizer,
    find_voice,
    piper_available,
    piper_sample_rate,
    piper_voice,
)
from athena.tts.qwen import QwenRealtimeSynthesizer
from athena.tts.sherpa import (
    SherpaSynthesizer,
    sherpa_available,
    sherpa_family,
    sherpa_sample_rate,
)

__all__ = [
    "EdgeSynthesizer",
    "PiperSynthesizer",
    "QwenRealtimeSynthesizer",
    "SherpaSynthesizer",
    "build_synthesizer",
    "tts_backend",
]

# `kitten` was this backend's original name and is kept working, because it is
# what the first documentation told people to set and a renamed setting that
# silently falls back to the cloud voice would look like the feature was removed.
SHERPA_ALIASES = {"sherpa", "kitten", "vits", "kokoro"}


def tts_backend() -> str:
    return os.environ.get("ATHENA_TTS_BACKEND", "qwen").strip().casefold() or "qwen"


def speech_is_billed(synthesizer=None) -> bool:
    """Whether the configured voice costs money per character.

    The coordinator prints a cost estimate after every reply, and printing a price
    next to a free voice reads as though the free voice is not really being used —
    which is exactly the thing a person switching to it is trying to confirm.
    """
    if synthesizer is not None:
        return isinstance(synthesizer, QwenRealtimeSynthesizer)
    return tts_backend() not in SHERPA_ALIASES | {"piper", "edge"}


def speech_cost_label(synthesizer=None) -> str:
    """What to print in place of a price, for a voice that costs nothing.

    Edge is free but it is not *local* — it is Microsoft's servers — and calling it
    local in the log would be the same kind of small lie as printing a price for
    it. The distinction matters when diagnosing: a network voice that goes quiet
    has a different cause from a local one.
    """
    if synthesizer is not None:
        return ("Edge voice, free" if isinstance(synthesizer, EdgeSynthesizer)
                else "local voice, free")
    return "Edge voice, free" if tts_backend() == "edge" else "local voice, free"


def build_synthesizer(settings, settings_store=None):
    """Return the configured synthesizer.

    `edge` is the best voice per unit of effort: Microsoft's neural voices, free,
    with no account or key. `sherpa` and `piper` speak locally and cost nothing but
    sound synthetic; `qwen` is the cloud voice and is billed per character.

    The order of preference is therefore edge, then a local voice, then the cloud.
    Each falls back rather than leaving ATHENA mute, and every fallback prints why
    — an unofficial service like Edge can stop working without notice, so a silent
    substitution would hide the one failure worth knowing about.
    """
    backend = tts_backend()
    if backend == "edge":
        available, reason = edge_available()
        if available:
            return EdgeSynthesizer(settings=settings_store)
        print(f"Edge speech was requested but is not usable ({reason}); "
              "falling back.", flush=True)
    elif backend in SHERPA_ALIASES:
        available, reason = sherpa_available()
        if available:
            return SherpaSynthesizer(settings=settings_store)
        print(f"Local speech was requested but is not usable ({reason}); "
              "using the cloud voice.", flush=True)
    elif backend == "piper":
        available, reason = piper_available()
        if available:
            return PiperSynthesizer(settings=settings_store)
        print(f"Piper was requested but is not usable ({reason}); using the cloud voice.",
              flush=True)
    return QwenRealtimeSynthesizer(
        settings.dashscope_api_key,
        settings.tts_model,
        settings.tts_voice,
        settings=settings_store,
    )


def synthesizer_sample_rate(settings) -> int:
    """The rate the speaker must be opened at for the chosen backend.

    Every backend has its own rate — Edge and the cloud voice are 24 kHz, a VITS
    Piper voice is 22.05 kHz — so this follows the backend rather than a single
    fixed setting. Getting it wrong is not silence: it is the same audio played at
    the wrong speed, which is much harder to attribute.
    """
    backend = tts_backend()
    if backend == "edge" and edge_available()[0]:
        return edge_sample_rate()
    if backend in SHERPA_ALIASES and sherpa_available()[0]:
        return sherpa_sample_rate()
    if backend == "piper" and piper_available()[0]:
        return piper_sample_rate()
    return settings.tts_sample_rate
