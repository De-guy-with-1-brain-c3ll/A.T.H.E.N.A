from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from athena.paths import data_directory


@dataclass(frozen=True, slots=True)
class SettingSpec:
    default: Any
    description: str
    value_type: type
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] = ()
    live: bool = False


CATALOG: dict[str, SettingSpec] = {
    "vad_minimum_rms": SettingSpec(
        400,
        "Microphone activation threshold. Higher is less sensitive.",
        int,
        100,
        2000,
        live=True,
    ),
    "vad_noise_multiplier": SettingSpec(
        2.7,
        "Required loudness above learned room noise. Higher is less sensitive.",
        float,
        1.2,
        6.0,
        live=True,
    ),
    "vad_end_silence_ms": SettingSpec(
        # 260 ms ended the turn in the middle of a sentence. A person pauses
        # 300-700 ms between clauses while thinking, and every one of those
        # pauses was being read as "finished talking" — which is what "the STT
        # cut me off" actually was. It is end-of-*speech* detection, not
        # end-of-sentence, so it has to outlast a breath. 750 ms is where
        # Benjamin landed after living with 600 and 1200: no more mid-sentence
        # cut-offs, without the full second of dead air after every command.
        750,
        "Silence before ending a command. Raise it if it cuts you off mid-sentence; "
        "lower it if it feels slow to respond.",
        int,
        200,
        2000,
        live=True,
    ),
    "vad_start_ms": SettingSpec(
        100,
        "Voiced milliseconds needed to start listening. Lower reacts sooner but is more noise-sensitive.",
        int,
        40,
        600,
        live=True,
    ),
    "vad_minimum_speech_ms": SettingSpec(
        180,
        "Voiced milliseconds required before a transcript is accepted. Lower accepts shorter words.",
        int,
        60,
        1200,
        live=True,
    ),
    "stt_language": SettingSpec(
        "en",
        "Recognition language. 'en,zh' recognises English with Chinese words mixed "
        "in, which is what misheard CJ as Jesus.",
        str,
        choices=("en", "zh", "ja", "ko", "en,zh", "zh,en"),
    ),
    "tts_voice": SettingSpec(
        "Neil",
        "Qwen real-time speaking voice. All six cost the same and respond in the "
        "same ~0.6s, so pick on sound rather than speed.",
        str,
        choices=("Neil", "Cherry", "Dolce", "Ethan", "Serena", "Chelsie"),
    ),
    "edge_voice": SettingSpec(
        "system",
        "Edge speaking voice. Ava is the most expressive; system uses ATHENA_EDGE_VOICE.",
        str,
        choices=("system", "en-US-AvaNeural", "en-US-JennyNeural", "en-US-AriaNeural"),
    ),
    "tts_speech_rate": SettingSpec(
        1.2, "Speaking speed: 1.0 is normal, 1.2 is twenty percent faster.",
        float, 0.5, 2.0, live=True
    ),
    "response_temperature": SettingSpec(
        0.2, "Response creativity. Lower is more predictable.", float, 0.0, 1.0, live=True
    ),
    "response_max_tokens": SettingSpec(
        160, "Maximum response length.", int, 40, 500, live=True
    ),
    "memory_enabled": SettingSpec(
        True, "Whether conversation memory is supplied and updated.", bool, live=True
    ),
    "memory_batch_delay_seconds": SettingSpec(
        1.5, "Delay used to batch background memory updates.", float, 0.2, 30.0
    ),
    "memory_summary_batch_size": SettingSpec(
        6, "Completed turns combined into one DeepSeek memory-summary request.",
        int, 3, 20, live=True
    ),
}


class RuntimeSettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_directory() / "settings.json"
        self._values: dict[str, Any] = {}
        self._stamp: tuple[int, int] | None = None
        self.reload()

    def reload(self) -> None:
        """Re-read settings only when the file actually changed.

        The voice loop reloads settings at the start of every listening turn.
        Parsing the file each time is wasted work on the critical path, so an
        unchanged file is skipped entirely.
        """
        try:
            stat = self.path.stat()
        except OSError:
            self._values = {}
            self._stamp = None
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        values: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in CATALOG:
                continue
            try:
                values[key] = self._validate(key, value)
            except (ValueError, TypeError):
                # One unreadable entry must not stop ATHENA from starting: fall
                # back to that setting's default and keep the rest.
                continue
        self._values = values
        self._stamp = stamp

    def get(self, name: str) -> Any:
        spec = self._spec(name)
        return self._values.get(name, spec.default)

    def set(self, name: str, value: Any) -> tuple[Any, bool]:
        validated = self._validate(name, value)
        self._values[name] = validated
        self._save()
        return validated, CATALOG[name].live

    def reset(self, name: str) -> tuple[Any, bool]:
        spec = self._spec(name)
        self._values.pop(name, None)
        self._save()
        return spec.default, spec.live

    def public_settings(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "value": self.get(name),
                "default": spec.default,
                "description": spec.description,
                "value_type": spec.value_type.__name__,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "choices": list(spec.choices),
                "applies_live": spec.live,
            }
            for name, spec in CATALOG.items()
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._values, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
        # The file changed, so the cached stamp is stale by definition.
        self._stamp = None

    @staticmethod
    def _spec(name: str) -> SettingSpec:
        try:
            return CATALOG[name]
        except KeyError as error:
            raise ValueError(f"Unknown or protected setting: {name}") from error

    def _validate(self, name: str, value: Any) -> Any:
        spec = self._spec(name)
        if spec.value_type is bool:
            if isinstance(value, bool):
                converted = value
            elif isinstance(value, str) and value.casefold() in {"true", "false"}:
                converted = value.casefold() == "true"
            else:
                raise ValueError(f"{name} must be true or false")
        elif spec.value_type is int:
            if isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            converted = int(value)
        elif spec.value_type is float:
            if isinstance(value, bool):
                raise ValueError(f"{name} must be a number")
            converted = float(value)
            if not math.isfinite(converted):
                raise ValueError(f"{name} must be a finite number")
        else:
            converted = str(value).strip()
        if spec.choices and converted not in spec.choices:
            raise ValueError(f"{name} must be one of: {', '.join(map(str, spec.choices))}")
        if spec.minimum is not None and converted < spec.minimum:
            raise ValueError(f"{name} must be at least {spec.minimum}")
        if spec.maximum is not None and converted > spec.maximum:
            raise ValueError(f"{name} must be at most {spec.maximum}")
        return converted
