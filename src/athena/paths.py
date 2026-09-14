"""Stable project and persistent-data locations across desktop and Pi installs."""
from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def environment_path(name: str, default: str | Path) -> Path:
    path = Path(os.environ.get(name, str(default)).strip()).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def data_directory() -> Path:
    return environment_path("ATHENA_DATA_DIR", "data")


def database_path() -> Path:
    return environment_path("ATHENA_DATABASE_PATH", data_directory() / "athena.db")
