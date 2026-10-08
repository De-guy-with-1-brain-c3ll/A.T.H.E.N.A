"""Pull, verify, install, and atomically activate a signed ATHENA release."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import zipfile

MAX_BUNDLE_BYTES = 50 * 1024 * 1024

# Creating a virtual environment copies the interpreter and runs ensurepip, which
# a Pi Zero 3's CPU and SD card cannot always finish inside two minutes. This is
# not a safety limit on untrusted work — it only bounds a local, root-owned
# `python -m venv` — so it is generous on purpose.
VENV_BUILD_TIMEOUT_SECONDS = 600


def signing_payload(manifest: dict) -> bytes:
    # The sequence is part of the signature, so an old but validly signed
    # manifest cannot be replayed to roll the Pi back to a weaker build.
    return (f"{manifest['schema']}\n{manifest['sequence']}\n{manifest['version']}\n"
            f"{manifest['archive']}\n{manifest['sha256']}\n{manifest['bytes']}\n").encode()


def fetch(url: str, maximum: int, timeout: float = 20) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("The update URL must be a plain HTTP or HTTPS address.")
    request = Request(url, headers={"User-Agent": "ATHENA-Pi-Updater/1", "Cache-Control": "no-cache"})
    with urlopen(request, timeout=timeout) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > maximum:
            raise ValueError("The update response is too large.")
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("The update response is too large.")
    return data


def discover_feed(configured: str, key: bytes) -> tuple[str, dict]:
    """Find the same signed feed after the development PC changes LAN IP."""
    parsed = urlsplit(configured)
    address = ipaddress.ip_address(parsed.hostname or "")
    if (address.version != 4 or not address.is_private or len(key) < 32
            or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Update feed discovery requires a private IPv4 address.")
    network = ipaddress.ip_network(f"{address}/24", strict=False)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    def probe(host):
        base = urlunsplit((parsed.scheme, f"{host}:{port}", parsed.path or "/", "", ""))
        try:
            manifest = json.loads(fetch(urljoin(base.rstrip("/") + "/", "manifest.json"),
                                        64 * 1024, timeout=0.8))
            expected = hmac.new(key, signing_payload(manifest), hashlib.sha256).hexdigest()
            if (manifest.get("schema") == 1
                    and hmac.compare_digest(expected, str(manifest.get("signature", "")))):
                return base, manifest
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    nearby = [ipaddress.ip_address(int(address) + offset)
              for distance in range(1, 9) for offset in (-distance, distance)
              if ipaddress.ip_address(int(address) + offset) in network]
    rest = [host for host in network.hosts() if host != address and host not in nearby]
    for candidates in (nearby, rest):
        with ThreadPoolExecutor(max_workers=min(32, len(candidates))) as pool:
            for match in pool.map(probe, map(str, candidates)):
                if match is not None:
                    return match
    raise ConnectionError("No signed ATHENA update feed was found on this LAN.")


def verify_release(manifest: dict, bundle: bytes, key: bytes) -> None:
    required = {"schema", "version", "archive", "sha256", "bytes", "signature", "sequence"}
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise ValueError("The update manifest is incomplete.")
    if manifest["schema"] != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(manifest["version"])):
        raise ValueError("The update manifest has an unsupported version format.")
    sequence = manifest["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("The update manifest has an invalid release sequence.")
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
                # Never trust the declared size alone: a zip bomb declares a
                # small file_size and then inflates far beyond it.
                with archive.open(member) as source, target.open("wb") as output:
                    remaining = member.file_size
                    while remaining > 0:
                        chunk = source.read(min(65536, remaining))
                        if not chunk:
                            break
                        output.write(chunk)
                        remaining -= len(chunk)
                    if remaining > 0 or source.read(1):
                        raise ValueError("The update archive contains a corrupted or inflated file.")
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
        # Building a venv copies the interpreter and runs ensurepip, which is
        # slow on a Pi Zero 3's CPU and SD card. Measured 2026-09-18: it exceeded
        # 120 seconds and the update failed with a bare timeout *after* the
        # release had already downloaded and verified — so the fix looked like a
        # bad download rather than a short clock. The pip step below already had
        # 900 seconds for the same reason.
        subprocess.run([sys.executable, "-m", "venv", str(release / ".venv")],
                       check=True, timeout=VENV_BUILD_TIMEOUT_SECONDS)
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
        record_state(root, manifest)
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


def installed_state(root: Path) -> dict:
    """Last installed release, used to refuse downgrades and replays."""
    path = root / "state.json"
    if path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                return state
        except (OSError, ValueError):
            pass
    return {"version": current_version(root), "sequence": 0}


def record_state(root: Path, manifest: dict) -> None:
    path = root / "state.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": str(manifest["version"]),
                                     "sequence": int(manifest["sequence"])},
                                    indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ATHENA_UPDATE_URL", ""))
    parser.add_argument("--root", type=Path, default=Path("/opt/athena"))
    parser.add_argument("--service", default=os.environ.get("ATHENA_UPDATE_SERVICE", "athena-feishu.service"))
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="Install even if the release is not newer than the installed one.")
    args = parser.parse_args()
    if not args.url:
        raise ValueError("Set ATHENA_UPDATE_URL to your computer's update-feed address.")
    key = os.environ.get("ATHENA_UPDATE_KEY", "").encode()
    root = args.root.resolve()
    with update_lock(root):
        manifest_url = urljoin(args.url.rstrip("/") + "/", "manifest.json")
        try:
            manifest = json.loads(fetch(manifest_url, 64 * 1024))
        except OSError:
            feed_url, manifest = discover_feed(args.url, key)
            manifest_url = urljoin(feed_url.rstrip("/") + "/", "manifest.json")
            print(f"Found signed ATHENA feed at {feed_url} after the PC address changed.")
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
        bundle = fetch(urljoin(manifest_url, archive), MAX_BUNDLE_BYTES)
        verify_release(manifest, bundle, key)
        # Checked only after the signature: an unauthenticated sequence is
        # just an attacker-controlled number.
        state = installed_state(root)
        installed_sequence = int(state.get("sequence", 0) or 0)
        if not args.force and int(manifest["sequence"]) <= installed_sequence:
            raise ValueError(
                f"The feed offers release {version or '?'} with sequence "
                f"{manifest['sequence']}, which is not newer than the installed "
                f"sequence {installed_sequence}. Refusing to downgrade or replay "
                "an older signed release; pass --force only if you are certain.")
        install_release(root, manifest, bundle, args.service)
        print(f"ATHENA {version} installed and {args.service} restarted.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ATHENA update failed: {error}", file=sys.stderr)
        raise SystemExit(1)
