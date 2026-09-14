"""Watch ATHENA source, publish signed builds, and serve them to the Pi."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import secrets
import socket
import threading
import time

from publish_update import build_release, source_files
from serve_updates import FeedHandler
from functools import partial
from http.server import ThreadingHTTPServer


def load_key(orange_dir: Path) -> tuple[bytes, bool]:
    supplied = os.environ.get("ATHENA_UPDATE_KEY", "").strip()
    if supplied:
        return supplied.encode(), False
    path = orange_dir / ".update-key"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip().encode(), False
    key = secrets.token_urlsafe(48).encode()
    path.write_text(key.decode() + "\n", encoding="utf-8")
    return key, True


def fingerprint(project_root: Path) -> str:
    digest = hashlib.sha256()
    for path in source_files(project_root):
        digest.update(path.relative_to(project_root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("8.8.8.8", 80))
            return connection.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def publish(project_root: Path, feed: Path, key: bytes) -> None:
    version = datetime.now(timezone.utc).strftime("%Y%m%d.%H%M%S")
    manifest = build_release(project_root, feed, version, key)
    print(f"Published ATHENA {manifest['version']} ({manifest['bytes']} bytes).")


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    orange_dir = project_root / "orange_pi"
    feed = orange_dir / "update_feed"
    key, created = load_key(orange_dir)
    if len(key) < 32:
        raise ValueError("ATHENA_UPDATE_KEY must contain at least 32 characters.")
    publish(project_root, feed, key)

    def watch() -> None:
        previous = fingerprint(project_root)
        while True:
            time.sleep(2)
            try:
                current = fingerprint(project_root)
                if current != previous:
                    time.sleep(1)
                    publish(project_root, feed, key)
                    previous = fingerprint(project_root)
            except Exception as error:
                print(f"Could not publish changed source: {error}")

    threading.Thread(target=watch, name="athena-update-watch", daemon=True).start()
    address, port = lan_address(), 8765
    print(f"Pi feed URL: http://{address}:{port}/")
    if created:
        print("New update key; copy this once into /etc/athena/update.env on the Pi:")
        print(key.decode())
    print("Watching for source changes. Press Ctrl+C to stop.")
    server = ThreadingHTTPServer(("0.0.0.0", port), partial(FeedHandler, directory=str(feed)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
