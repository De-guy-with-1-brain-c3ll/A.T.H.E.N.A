from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from athena.paths import database_path, environment_path
from athena.settings.store import RuntimeSettingsStore


# systemd loads this into the Pi's services. Reading it here as well lets the
# command-line tools work on a Pi that is configured but running no shell
# environment of its own.
SYSTEM_ENV_FILE = Path("/etc/athena/athena.env")


def load_local_environment() -> None:
    """Load missing variables from the project-local .env file.

    On the Pi the real configuration lives in /etc/athena/athena.env, which
    systemd injects for the services. A tool run by hand from a shell has no
    systemd environment, so that file is read as a fallback too. Without it
    every command-line tool reported "not configured" on a fully configured Pi.
    """
    for path in (Path(__file__).resolve().parents[2] / ".env", SYSTEM_ENV_FILE):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw_line in text.splitlines():
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


# DashScope real-time speech recognition, researched 2026-09-16.
#
# Billing is per second of INPUT AUDIO; the transcript itself is not charged, so
# there is no such thing as an STT "token" cost. The lever is how many seconds of
# audio reach the service and which model receives them. Prices are China
# (Beijing) list prices in CNY per audio second, each with a 36,000 second
# (10 hour) free quota valid for 90 days.
#
# Measured 2026-09-17 against the live service. The two working English models
# were statistically tied on word error rate over six sentences (0.114 vs 0.112),
# so the choice does not come down to accuracy. `qwen3-asr-flash-realtime` was
# listed here as a choice but the service answers "Model not found" for it, so it
# is not offered: a dropdown entry that fails on selection is worse than no entry.
ASR_MODELS: dict[str, tuple[float, str]] = {
    "qwen-audio-3.0-asr-flash-streaming": (
        0.00033, "Newest generation, multilingual plus dialects, any sample rate. Default."),
    "fun-asr-realtime": (
        0.00033, "Multilingual; also speaks the AOQ protocol for weak networks."),
    "fun-asr-flash-8k-realtime": (
        0.00022, "Cheapest real-time option, but Chinese only and 8 kHz."),
}

# Local speech recognition: no per-second billing and no network round trip.
# Measured 2026-09-17 with sherpa-onnx and SenseVoice-Small at 45-75x realtime on
# a desktop, and 22x on a single thread — comfortably faster than speech, so the
# cost of a turn is the model load, not the decoding. The int8 model is a quarter
# of the size and about 2.5x faster than fp32 for a small word-error-rate cost,
# which is the right trade on a 4 GB board.
SENSEVOICE_MODEL_URL = (
    "https://hf-mirror.com/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main")


@dataclass(frozen=True, slots=True)
class Settings:
    dashscope_api_key: str
    deepseek_api_key: str
    stt_model: str = "fun-asr-realtime"
    stt_language: str = "en"
    tts_model: str = "qwen3-tts-flash-realtime"
    # Qwen's natural, laid-back Italian voice.  Keep this as the fallback so
    # fresh installs use the requested voice even before runtime settings exist.
    tts_voice: str = "Dolce"
    deepseek_model: str = "deepseek-v4-flash"
    # Voice can use a faster provider independently of the full DeepSeek
    # agent.  Memory, text chat, and tools deliberately retain DeepSeek: they
    # depend on its existing tool-call behaviour and do not sit on the spoken
    # first-audio path.
    voice_llm_provider: str = "deepseek"
    voice_llm_model: str = "deepseek-v4-flash"
    stt_sample_rate: int = 16_000
    tts_sample_rate: int = 24_000
    # Keep a DashScope speech session warm between turns so the websocket
    # handshake is not paid after the user has already started talking.
    stt_prewarm: bool = True
    # Ask the service to finalise a sentence as soon as the local gate does.
    # None leaves the service default in place.
    # Let the streaming service finalise when the local gate has closed the
    # input.  Live paced testing found that overriding this can delay the final
    # transcript by seconds, even when it matches the VAD window.
    stt_max_sentence_silence_ms: int | None = None
    # Semantic punctuation makes the service ignore pauses when deciding where
    # a sentence ends, so finals arrive late and one utterance gets answered in
    # pieces — the "replies to the previous turn" complaint. Off by default:
    # a command assistant needs the final when the user stops talking, not
    # when the service decides the punctuation looks complete.
    stt_semantic_punctuation: bool = False
    # A single gated segment is capped at this many seconds of streamed audio.
    # Music, television or a noisy room can hold the gate open for minutes and
    # every one of those seconds is billed; the cap also forces a final
    # transcript, so a stuck segment answers instead of streaming forever.
    stt_segment_cap_seconds: float = 20.0
    # While music is playing the microphone hears the song, and the gate cannot
    # tell a lyric from a command: it streams the whole track and answers to
    # lyrics. Listening is suppressed during playback unless explicitly asked
    # for, because it is the single biggest source of surprise billing.
    listen_while_music: bool = False
    audio_input_device: str | None = None
    audio_output_device: str | None = None
    audio_debug: bool = False
    database_path: Path = Path("data/athena.db")
    memory_batch_delay_seconds: float = 1.5
    vad_minimum_rms: int = 400
    vad_noise_multiplier: float = 2.7
    vad_end_silence_ms: int = 750

    @property
    def voice_llm_api_key(self) -> str:
        return self.dashscope_api_key if self.voice_llm_provider == "qwen" else self.deepseek_api_key

    @property
    def voice_llm_base_url(self) -> str:
        if self.voice_llm_provider == "qwen":
            return "https://dashscope.aliyuncs.com/compatible-mode/v1"
        return "https://api.deepseek.com"

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
        voice_provider = os.environ.get("ATHENA_VOICE_LLM_PROVIDER", "deepseek").strip().casefold()
        if voice_provider not in {"deepseek", "qwen"}:
            raise ValueError("ATHENA_VOICE_LLM_PROVIDER must be 'deepseek' or 'qwen'.")
        deepseek_model = (
            os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
            or "deepseek-v4-flash"
        )
        voice_default_model = "qwen-turbo" if voice_provider == "qwen" else deepseek_model
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
            deepseek_model=deepseek_model,
            voice_llm_provider=voice_provider,
            voice_llm_model=(
                os.environ.get("ATHENA_VOICE_LLM_MODEL", voice_default_model).strip()
                or voice_default_model
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
            stt_prewarm=os.environ.get("ATHENA_STT_PREWARM", "1").strip().casefold()
            in {"1", "true", "yes", "on"},
            stt_max_sentence_silence_ms=(
                int(os.environ.get("ATHENA_STT_MAX_SENTENCE_SILENCE_MS",
                    str(runtime.get("vad_end_silence_ms") if runtime else 750))) or None
            ),
            stt_semantic_punctuation=os.environ.get(
                "ATHENA_STT_SEMANTIC_PUNCTUATION", "0").strip().casefold()
            in {"1", "true", "yes", "on"},
            stt_segment_cap_seconds=(
                float(os.environ.get("ATHENA_STT_SEGMENT_CAP_SECONDS", "20")) or 20.0
            ),
            listen_while_music=os.environ.get(
                "ATHENA_LISTEN_WHILE_MUSIC", "0").strip().casefold()
            in {"1", "true", "yes", "on"},
            tts_voice=runtime.get("tts_voice") if runtime else "Dolce",
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
                else int(os.environ.get("ATHENA_VAD_END_SILENCE_MS", "600"))
            ),
        )
