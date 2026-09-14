"""Serve signed ATHENA releases read-only to the Orange Pi over the LAN."""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FeedHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)
        return None


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--feed", type=Path, default=project_root / "orange_pi" / "update_feed")
    args = parser.parse_args()
    feed = args.feed.resolve()
    feed.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.bind, args.port), partial(FeedHandler, directory=str(feed)))
    print(f"ATHENA update feed: http://{args.bind}:{args.port}/manifest.json")
    print("Keep this window open while the Orange Pi checks for updates.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
