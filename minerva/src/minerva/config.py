"""Minerva configuration.

All values read from environment with sensible defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    model: str = "deepseek/deepseek-flash"
    openclaw_timeout_seconds: int = 120
    stt_model_dir: str | None = None
    tts_voice: str = "en_US-lessac-medium"
    log_level: str = "info"


def settings() -> Settings:
    return Settings(
        model=os.environ.get("MINERVA_MODEL", "deepseek/deepseek-flash"),
        openclaw_timeout_seconds=int(
            os.environ.get("MINERVA_OPENCLAW_TIMEOUT", "120")
        ),
        stt_model_dir=os.environ.get("MINERVA_STT_MODEL_DIR") or None,
        tts_voice=os.environ.get("MINERVA_TTS_VOICE", "en_US-lessac-medium"),
        log_level=os.environ.get("MINERVA_LOG_LEVEL", "info"),
    )
