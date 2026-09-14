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
        260,
        "Silence before ending a command. Lower responds faster but cuts pauses.",
        int,
        160,
        1200,
        live=True,
    ),
    "stt_language": SettingSpec(
        "en", "Recognition language.", str, choices=("en", "zh", "ja", "ko")
    ),
    "tts_voice": SettingSpec(
        "Neil", "Qwen real-time speaking voice.", str, choices=("Neil", "Cherry")
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
        self.reload()

    def reload(self) -> None:
        if not self.path.is_file():
            self._values = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        self._values = {
            key: self._validate(key, value)
            for key, value in raw.items()
            if key in CATALOG
        }

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
