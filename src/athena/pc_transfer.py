"""Authenticated LAN inbox. No remote execution or arbitrary destination paths."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import re
import time
import json
import logging
from uuid import uuid4

from aiohttp import web

MAX_BYTES = 25 * 1024 * 1024


def signature(key, timestamp, nonce, filename, digest):
    return hmac.new(key.encode(), f"{timestamp}\n{nonce}\n{filename}\n{digest}".encode(), hashlib.sha256).hexdigest()


def inbox_app(inbox, key, browser=None):
    if len(key) < 32:
        raise ValueError("ATHENA_PC_TRANSFER_KEY must be at least 32 characters.")
    inbox = Path(inbox).resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    seen = {}

    async def receive(request):
        try:
            address = ipaddress.ip_address(request.remote)
            if not (address.is_private or address.is_loopback):
                raise ValueError("LAN only")
            stamp = request.headers["X-Athena-Time"]
            nonce = request.headers["X-Athena-Nonce"]
            name = request.headers["X-Athena-Name"]
            digest = request.headers["X-Athena-SHA256"]
            if abs(time.time() - int(stamp)) > 120 or not re.fullmatch(r"[a-f0-9]{32}", nonce):
                raise ValueError("Expired request")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name) or name.endswith("."):
                raise ValueError("Invalid filename")
            if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
                raise ValueError("Reserved filename")
            signed_name = f"POST:/browser:{name}" if request.path == "/browser" else name
            expected = signature(key, stamp, nonce, signed_name, digest)
            if not hmac.compare_digest(expected, request.headers.get("X-Athena-Signature", "")):
                raise ValueError("Authentication failed")
            now = time.time()
            for old in list(seen):
                if seen[old] < now - 240:
                    del seen[old]
            if nonce in seen or len(seen) > 5000:
                raise ValueError("Replay or excessive requests")
            seen[nonce] = now
        except (ValueError, KeyError, TypeError):
            raise web.HTTPForbidden(text="Invalid signed LAN request.") from None
        if request.path == "/browser":
            if browser is None:
                raise web.HTTPServiceUnavailable(text="PC browser bridge is unavailable.")
            chunks, size = [], 0
            async for chunk in request.content.iter_chunked(1024):
                size += len(chunk)
                if size > 10000: raise web.HTTPBadRequest(text="Browser request too large.")
                chunks.append(chunk)
            raw = b"".join(chunks)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise web.HTTPBadRequest(text="Invalid browser request.")
            try:
                body = json.loads(raw)
                if not isinstance(body, dict): raise ValueError("Expected a browser action object.")
                return web.json_response(await browser.execute(body))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise web.HTTPBadRequest(text=str(error)) from None
            except Exception:
                logging.exception("PC browser action failed")
                raise web.HTTPBadGateway(text="PC browser action failed; check the PC bridge log.") from None
        # Never overwrite or execute received files. UUID prefix prevents both
        # collision and NTFS device names; the file is published only on success.
        target = inbox / f"{uuid4().hex[:10]}-{name}"
        temporary = inbox / f".{uuid4().hex}.partial"
        size, hashed = 0, hashlib.sha256()
        try:
            with temporary.open("xb") as output:
                async for chunk in request.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_BYTES, actual_size=size)
                    hashed.update(chunk)
                    output.write(chunk)
            if not hmac.compare_digest(hashed.hexdigest(), digest):
                raise web.HTTPBadRequest(text="File hash mismatch.")
            temporary.replace(target)
            return web.json_response({"filename": target.name, "bytes": size, "sha256": digest})
        finally:
            temporary.unlink(missing_ok=True)

    app = web.Application(client_max_size=MAX_BYTES)
    app.router.add_post("/upload", receive)
    app.router.add_post("/browser", receive)
    if browser is not None:
        async def close_browser(_app): await browser.close()
        app.on_cleanup.append(close_browser)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1", help="Use your PC's LAN IPv4 address for Pi access.")
    parser.add_argument("--port", type=int, default=8781)
    parser.add_argument("--inbox", type=Path, default=Path.cwd() / "ATHENA Inbox")
    args = parser.parse_args()
    if not ipaddress.ip_address(args.bind).is_private:
        parser.error("Bind to a private LAN address or localhost, not a public interface.")
    from athena.pc_browser import PCBrowser
    browser = PCBrowser(args.inbox.parent / "data" / "pc-browser-profile")
    web.run_app(inbox_app(args.inbox, os.environ.get("ATHENA_PC_TRANSFER_KEY", ""), browser),
                host=args.bind, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
