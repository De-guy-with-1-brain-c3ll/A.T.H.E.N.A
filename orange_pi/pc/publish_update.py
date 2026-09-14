"""Build and sign an ATHENA source release for an Orange Pi update feed."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile


def signing_payload(manifest: dict) -> bytes:
    return (f"{manifest['schema']}\n{manifest['version']}\n{manifest['archive']}\n"
            f"{manifest['sha256']}\n{manifest['bytes']}\n").encode()


def source_files(project_root: Path) -> list[Path]:
    files = [project_root / "pyproject.toml", project_root / ".env.example",
             project_root / "orange_pi" / "VERSION"]
    files.extend(path for path in (project_root / "src" / "athena").rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts
                 and path.suffix not in {".pyc", ".pyo"})
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing release file: " + missing[0])
    return sorted(set(files), key=lambda path: path.relative_to(project_root).as_posix())


def build_release(project_root: Path, feed: Path, version: str, key: bytes) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise ValueError("Version must use only letters, numbers, dots, dashes and underscores.")
    if len(key) < 32:
        raise ValueError("ATHENA_UPDATE_KEY must contain at least 32 characters.")
    feed.mkdir(parents=True, exist_ok=True)
    archive_name = f"athena-{version}.zip"
    archive = feed / archive_name
    with tempfile.NamedTemporaryFile(dir=feed, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for path in source_files(project_root):
                relative = path.relative_to(project_root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                content = (version.encode() + b"\n" if relative == "orange_pi/VERSION"
                           else path.read_bytes())
                bundle.writestr(info, content)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        manifest = {"schema": 1, "version": version, "archive": archive_name,
                    "sha256": digest, "bytes": temporary.stat().st_size}
        manifest["signature"] = hmac.new(key, signing_payload(manifest), hashlib.sha256).hexdigest()
        temporary.replace(archive)
        manifest_tmp = feed / "manifest.json.tmp"
        manifest_tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        manifest_tmp.replace(feed / "manifest.json")
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=(project_root / "orange_pi" / "VERSION").read_text().strip())
    parser.add_argument("--feed", type=Path, default=project_root / "orange_pi" / "update_feed")
    args = parser.parse_args()
    key = os.environ.get("ATHENA_UPDATE_KEY", "").encode()
    manifest = build_release(project_root, args.feed.resolve(), args.version, key)
    print(f"Published {manifest['archive']} ({manifest['bytes']} bytes).")
    print("The Pi can pull it after the update server is running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
