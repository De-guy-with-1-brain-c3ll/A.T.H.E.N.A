"""Restore and build the pinned ATHENA OpenClaw fork; never replace the live gateway."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent.parent
FORK = ROOT / "openclaw-fork"
DATA = ROOT / "data" / "openclaw"


def run(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", action="store_true", help="Restore missing source from the pinned archive")
    parser.add_argument("--install", action="store_true", help="Install pinned pnpm and locked dependencies")
    parser.add_argument("--test", action="store_true", help="Run the focused build regression tests before building")
    args = parser.parse_args()
    lock = json.loads((FORK / "source-lock.json").read_text(encoding="utf-8"))
    source = DATA / "source"
    archive = DATA / "source.zip"
    if not (source / "package.json").is_file():
        if not args.restore:
            parser.error("Source missing. Run with --restore --install.")
        DATA.mkdir(parents=True, exist_ok=True)
        if not archive.is_file():
            url = "https://codeload.github.com/openclaw/openclaw/zip/" + lock["commit"]
            urllib.request.urlretrieve(url, archive)
        if digest(archive) != lock["archiveSha256"]:
            raise RuntimeError("Pinned source archive checksum mismatch")
        with zipfile.ZipFile(archive) as zipped:
            prefix = zipped.namelist()[0].split("/", 1)[0] + "/"
            for member in zipped.infolist():
                relative = Path(member.filename.removeprefix(prefix))
                target = (source / relative).resolve()
                if not target.is_relative_to(source.resolve()):
                    raise RuntimeError("Archive member escapes source checkout")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zipped.read(member))
    if json.loads((source / "package.json").read_text())["version"] != lock["tag"].removeprefix("v"):
        raise RuntimeError("Source version does not match source-lock.json")
    if digest(source / "pnpm-lock.yaml") != lock["lockfileSha256"]:
        raise RuntimeError("Source dependency lock differs from the pinned release")
    ready = source / ".athena-build-ready.json"
    ready.unlink(missing_ok=True)
    env = os.environ.copy()
    # Reproducible upstream identity for archive checkouts, without Git blob hydration.
    env["GIT_COMMIT"] = lock["commit"]
    node_dir = DATA / ("node-v" + lock["node"] + "-win-x64")
    node = node_dir / "node.exe"
    if not node.is_file():
        raise RuntimeError("Pinned Node runtime missing: " + str(node))
    env["PATH"] = str(node_dir) + os.pathsep + env.get("PATH", "")
    for patch_name in lock["patches"]:
        patch = str(FORK / patch_name)
        checked = subprocess.run(["git", "apply", "--check", patch], cwd=source, env=env, capture_output=True)
        if checked.returncode == 0:
            run(["git", "apply", patch], source, env)
        else:
            applied = subprocess.run(["git", "apply", "--reverse", "--check", patch], cwd=source, env=env, capture_output=True)
            if applied.returncode:
                raise RuntimeError("Patch cannot be applied or verified: " + patch_name)
    tool_root = DATA / "build-tools"
    pnpm = tool_root / "node_modules" / "pnpm" / "bin" / "pnpm.mjs"
    if args.install:
        npm = node_dir / "node_modules" / "npm" / "bin" / "npm-cli.js"
        run([str(node), str(npm), "install", "--prefix", str(tool_root), "--no-audit", "--no-fund", "pnpm@" + lock["pnpm"]], ROOT, env)
    if not pnpm.is_file():
        raise RuntimeError("Pinned pnpm missing. Run with --install.")
    if json.loads((pnpm.parent.parent / "package.json").read_text())["version"] != lock["pnpm"]:
        raise RuntimeError("Build pnpm differs from the pinned toolchain")
    if args.install:
        run([str(node), str(pnpm), "install", "--frozen-lockfile"], source, env)
    if args.test:
        run([str(node), "scripts/run-vitest.mjs", "test/scripts/build-all.test.ts"], source, env)
    run([str(node), str(pnpm), "build"], source, env)
    if not (source / "dist" / "entry.js").is_file():
        raise RuntimeError("Build exited without the Gateway entry artifact")
    run([str(node), "openclaw.mjs", "--version"], source, env)
    manifest = {
        "version": lock["tag"].removeprefix("v"),
        "commit": lock["commit"],
        "fullBuildPassed": True,
        "entrySha256": digest(source / "dist" / "entry.js"),
        "patches": {name: digest(FORK / name) for name in lock["patches"]},
        "checks": ["pnpm build", "openclaw --version"] + (["77 build regression tests"] if args.test else []),
        "builtAt": datetime.now(timezone.utc).isoformat(),
    }
    temporary = ready.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(ready)
    print("Built pinned source runtime: " + str(source / "openclaw.mjs"))
    print("The running Gateway and rollback runtime were not replaced.")


if __name__ == "__main__":
    main()
