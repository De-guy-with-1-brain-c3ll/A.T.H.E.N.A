"""Editable prompts that survive signed application updates."""
from __future__ import annotations

import os
from pathlib import Path

from athena.paths import data_directory


_LIMITS = {"system": 20_000, "memory": 10_000}


def packaged_prompt_path(name: str) -> Path:
    if name not in _LIMITS:
        raise ValueError("Unknown prompt name.")
    return Path(__file__).resolve().parent / "system" / f"{name}_prompt.txt"


def editable_prompt_path(name: str) -> Path:
    if name not in _LIMITS:
        raise ValueError("Unknown prompt name.")
    variable = f"ATHENA_{name.upper()}_PROMPT_PATH"
    configured = os.environ.get(variable, "").strip()
    return Path(configured) if configured else data_directory() / "prompts" / f"{name}_prompt.txt"


def read_prompt(name: str) -> str:
    editable = editable_prompt_path(name)
    source = editable if editable.is_file() else packaged_prompt_path(name)
    return source.read_text(encoding="utf-8").strip()


def write_prompt(name: str, content: str) -> Path:
    if name not in _LIMITS:
        raise ValueError("Unknown prompt name.")
    content = content.replace("\r\n", "\n").strip()
    if not content:
        raise ValueError("The prompt cannot be empty.")
    if len(content) > _LIMITS[name]:
        raise ValueError(f"The {name} prompt is too long (maximum {_LIMITS[name]:,} characters).")
    target = editable_prompt_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".new")
    temporary.write_text(content + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target
