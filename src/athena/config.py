from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from athena.paths import database_path, environment_path
from athena.settings.store import RuntimeSettingsStore


def load_local_environment() -> None:
    """Load missing variables from the project-local .env file."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


def project_path_from_environment(name: str, default: str) -> Path:
    """Resolve relative user paths against the ATHENA project, not the caller's CWD."""
    return environment_path(name, default)


@dataclass(frozen=True, slots=True)
class Settings:
    dashscope_api_key: str
    deepseek_api_key: str
    stt_model: str = "fun-asr-realtime"
    stt_language: str = "en"
    tts_model: str = "qwen3-tts-flash-realtime"
    tts_voice: str = "Bellona"
    deepseek_model: str = "deepseek-v4-flash"
    stt_sample_rate: int = 16_000
    tts_sample_rate: int = 24_000
    audio_input_device: str | None = None
    audio_output_device: str | None = None
    audio_debug: bool = False
    database_path: Path = Path("data/athena.db")
    memory_batch_delay_seconds: float = 1.5
    vad_minimum_rms: int = 300
    vad_noise_multiplier: float = 2.2
    vad_end_silence_ms: int = 220

    @classmethod
    def from_environment(
        cls, runtime: RuntimeSettingsStore | None = None
    ) -> "Settings":
        load_local_environment()
        dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        missing = [
            name
            for name, value in (
                ("DASHSCOPE_API_KEY", dashscope_key),
                ("DEEPSEEK_API_KEY", deepseek_key),
            )
            if not value
        ]
        if missing:
            raise ValueError("Missing environment variable(s): " + ", ".join(missing))
        resolved_database_path = database_path()
        return cls(
            dashscope_api_key=dashscope_key,
            deepseek_api_key=deepseek_key,
            database_path=resolved_database_path,
            stt_model=(
                os.environ.get("ATHENA_STT_MODEL", "qwen-audio-3.0-asr-flash-streaming").strip()
                or "fun-asr-realtime"
            ),
            tts_model=(
                os.environ.get("ATHENA_TTS_MODEL", "qwen3-tts-flash-realtime").strip()
                or "qwen3-tts-flash-realtime"
            ),
            deepseek_model=(
                os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
                or "deepseek-v4-flash"
            ),
            audio_input_device=os.environ.get("ATHENA_AUDIO_INPUT_DEVICE", "").strip() or None,
            audio_output_device=os.environ.get("ATHENA_AUDIO_OUTPUT_DEVICE", "").strip() or None,
            audio_debug=os.environ.get("ATHENA_AUDIO_DEBUG", "0").strip().casefold()
            in {"1", "true", "yes", "on"},
            stt_language=(
                runtime.get("stt_language")
                if runtime
                else os.environ.get("ATHENA_STT_LANGUAGE", "en").strip() or "en"
            ),
            tts_voice=runtime.get("tts_voice") if runtime else "Neil",
            memory_batch_delay_seconds=(
                runtime.get("memory_batch_delay_seconds") if runtime else 1.5
            ),
            vad_minimum_rms=(
                runtime.get("vad_minimum_rms")
                if runtime
                else int(os.environ.get("ATHENA_VAD_MINIMUM_RMS", "400"))
            ),
            vad_noise_multiplier=(
                runtime.get("vad_noise_multiplier")
                if runtime
                else float(os.environ.get("ATHENA_VAD_NOISE_MULTIPLIER", "2.7"))
            ),
            vad_end_silence_ms=(
                runtime.get("vad_end_silence_ms")
                if runtime
                else int(os.environ.get("ATHENA_VAD_END_SILENCE_MS", "260"))
            ),
        )
