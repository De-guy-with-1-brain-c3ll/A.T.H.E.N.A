"""Pull, verify, install, and atomically activate a signed ATHENA release."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import zipfile

MAX_BUNDLE_BYTES = 50 * 1024 * 1024


def signing_payload(manifest: dict) -> bytes:
    return (f"{manifest['schema']}\n{manifest['version']}\n{manifest['archive']}\n"
            f"{manifest['sha256']}\n{manifest['bytes']}\n").encode()


def fetch(url: str, maximum: int) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("The update URL must be a plain HTTP or HTTPS address.")
    request = Request(url, headers={"User-Agent": "ATHENA-Pi-Updater/1", "Cache-Control": "no-cache"})
    with urlopen(request, timeout=20) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > maximum:
            raise ValueError("The update response is too large.")
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("The update response is too large.")
    return data


def verify_release(manifest: dict, bundle: bytes, key: bytes) -> None:
    required = {"schema", "version", "archive", "sha256", "bytes", "signature"}
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise ValueError("The update manifest is incomplete.")
    if manifest["schema"] != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(manifest["version"])):
        raise ValueError("The update manifest has an unsupported version format.")
    if not re.fullmatch(r"athena-[A-Za-z0-9._-]+\.zip", str(manifest["archive"])):
        raise ValueError("The update archive name is invalid.")
    if len(key) < 32:
        raise ValueError("ATHENA_UPDATE_KEY must contain at least 32 characters.")
    if len(bundle) != int(manifest["bytes"]):
        raise ValueError("The update size does not match the signed manifest.")
    digest = hashlib.sha256(bundle).hexdigest()
    if not hmac.compare_digest(digest, str(manifest["sha256"])):
        raise ValueError("The update checksum is invalid.")
    expected = hmac.new(key, signing_payload(manifest), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(manifest["signature"])):
        raise ValueError("The update signature is invalid.")


def extract_bundle(bundle: bytes, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        members = archive.infolist()
        if len(members) > 500:
            raise ValueError("The update contains too many files.")
        total = 0
        for member in members:
            name = PurePosixPath(member.filename)
            total += member.file_size
            mode = member.external_attr >> 16
            if (name.is_absolute() or ".." in name.parts or not name.parts
                    or member.file_size > 5 * 1024 * 1024 or total > MAX_BUNDLE_BYTES
                    or (mode & 0o170000) == 0o120000):
                raise ValueError("The update archive contains an unsafe path or file.")
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not member.is_dir():
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
    if not (destination / "pyproject.toml").is_file() or not (destination / "src" / "athena" / "__init__.py").is_file():
        raise ValueError("The update is missing ATHENA program files.")


@contextmanager
def update_lock(root: Path):
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    with (root / "update.lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another ATHENA update is already running.") from error
        yield


def switch_link(current: Path, release: Path) -> Path | None:
    if current.exists() and not current.is_symlink():
        raise ValueError("The current ATHENA path is not a managed link.")
    previous = current.resolve() if current.is_symlink() else None
    temporary = current.with_name(".current-new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(release, target_is_directory=True)
    os.replace(temporary, current)
    return previous


def restart_service(service: str) -> None:
    if not re.fullmatch(r"athena-(?:feishu|voice)\.service", service):
        raise ValueError("ATHENA_UPDATE_SERVICE must be athena-feishu.service or athena-voice.service.")
    subprocess.run(["systemctl", "restart", service], check=True, timeout=30)
    subprocess.run(["systemctl", "is-active", "--quiet", service], check=True, timeout=10)


def restart_dashboard() -> bool:
    """Restart the dashboard after its release path changes, when installed."""
    unit = Path("/etc/systemd/system/athena-web.service")
    if not unit.is_file():
        return False
    enabled = subprocess.run(
        ["systemctl", "is-enabled", "--quiet", "athena-web.service"],
        check=False, timeout=10,
    )
    if enabled.returncode != 0:
        return False
    subprocess.run(["systemctl", "restart", "athena-web.service"], check=True, timeout=30)
    subprocess.run(["systemctl", "is-active", "--quiet", "athena-web.service"],
                   check=True, timeout=10)
    return True


def pip_install_command(python: Path, root: Path, release: Path) -> list[str]:
    # Never use editable mode here: its absolute source pointer would make the
    # release dependent on a temporary path.
    return [str(python), "-m", "pip", "install", "--prefer-binary",
            "--cache-dir", str(root / "cache"), str(release)]


def prune_releases(root: Path, keep: set[Path]) -> None:
    """Keep the active and previous release; delete older inactive releases."""
    releases = root / "releases"
    protected = {path.resolve() for path in keep if path is not None}
    for path in sorted(releases.iterdir(), key=lambda item: item.stat().st_mtime,
                       reverse=True):
        if (not path.is_dir() or path.is_symlink() or path.name.startswith(".")
                or path.resolve() in protected):
            continue
        shutil.rmtree(path)


def install_release(root: Path, manifest: dict, bundle: bytes, service: str) -> None:
    if not root.is_absolute() or root in {Path("/"), Path("/opt"), Path("/usr"), Path("/home")}:
        raise ValueError("Choose a dedicated absolute ATHENA installation directory.")
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / str(manifest["version"])
    if release.exists():
        raise ValueError("That release is already installed but is not active; inspect it before removing it.")
    staging = releases / f".{manifest['version']}.staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    previous = None
    try:
        extract_bundle(bundle, staging)
        # Move the source into its permanent absolute path before creating the
        # venv because console-script shebangs contain that absolute path.
        staging.rename(release)
        subprocess.run([sys.executable, "-m", "venv", str(release / ".venv")], check=True, timeout=120)
        python = release / ".venv" / "bin" / "python"
        subprocess.run(pip_install_command(python, root, release), check=True, timeout=900)
        subprocess.run([str(python), "-m", "compileall", "-q", str(release / "src")],
                       check=True, timeout=60)
        smoke = (
            "from pathlib import Path; import athena; "
            "from athena.tools.registry import ToolRegistry; "
            "root=Path(athena.__file__).parent; "
            "assert (root/'system'/'system_prompt.txt').is_file(); "
            "assert (root/'system'/'memory_prompt.txt').is_file(); "
            "assert ToolRegistry.discover().names()"
        )
        subprocess.run([str(python), "-c", smoke],
                       cwd=release, check=True, timeout=60)
        previous = switch_link(root / "current", release)
        try:
            restart_service(service)
            restart_dashboard()
        except Exception:
            if previous and previous.is_dir():
                switch_link(root / "current", previous)
                restart_service(service)
                restart_dashboard()
            raise
        try:
            prune_releases(root, {release, previous} if previous else {release})
        except OSError as error:
            print(f"ATHENA updated, but old-release cleanup failed: {error}", file=sys.stderr)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        current = root / "current"
        release_is_current = current.is_symlink() and current.resolve() == release.resolve()
        if release.exists() and not release_is_current:
            shutil.rmtree(release)


def current_version(root: Path) -> str:
    version_file = root / "current" / "orange_pi" / "VERSION"
    return version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ATHENA_UPDATE_URL", ""))
    parser.add_argument("--root", type=Path, default=Path("/opt/athena"))
    parser.add_argument("--service", default=os.environ.get("ATHENA_UPDATE_SERVICE", "athena-feishu.service"))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if not args.url:
        raise ValueError("Set ATHENA_UPDATE_URL to your computer's update-feed address.")
    key = os.environ.get("ATHENA_UPDATE_KEY", "").encode()
    root = args.root.resolve()
    with update_lock(root):
        manifest_url = urljoin(args.url.rstrip("/") + "/", "manifest.json")
        manifest = json.loads(fetch(manifest_url, 64 * 1024))
        version = str(manifest.get("version", ""))
        if version == current_version(root):
            print(f"ATHENA {version} is already current.")
            return 0
        print(f"ATHENA update available: {version or 'invalid manifest'}.")
        if args.check_only:
            return 10
        archive = str(manifest.get("archive", ""))
        if not re.fullmatch(r"athena-[A-Za-z0-9._-]+\.zip", archive):
            raise ValueError("The update archive name is invalid.")
        if len(key) < 32:
            raise ValueError("ATHENA_UPDATE_KEY must contain at least 32 characters.")
        bundle = fetch(urljoin(args.url.rstrip("/") + "/", archive), MAX_BUNDLE_BYTES)
        verify_release(manifest, bundle, key)
        install_release(root, manifest, bundle, args.service)
        print(f"ATHENA {version} installed and {args.service} restarted.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ATHENA update failed: {error}", file=sys.stderr)
        raise SystemExit(1)
