"""Find the authenticated PC inbox after its LAN address changes."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import aiohttp


def proof(key: str, nonce: str) -> str:
    return hmac.new(key.encode(), f"ATHENA-PC-INBOX\n{nonce}".encode(),
                    hashlib.sha256).hexdigest()


async def resolve_pc_url(configured: str, key: str) -> str:
    """Return only an inbox that proves possession of the configured shared key.

    The original address anchors a private /24 scan. No file bytes or key are
    sent to candidates; the nonce challenge is verified before an upload.
    """
    parsed = urlsplit(configured)
    address = ipaddress.ip_address(parsed.hostname or "")
    if (address.version != 4 or not address.is_private or len(key) < 32
            or parsed.scheme not in {"http", "https"} or parsed.path != "/upload"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("The PC inbox configuration is invalid.")
    nonce = uuid4().hex
    expected = proof(key, nonce)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    timeout = aiohttp.ClientTimeout(total=0.8, connect=0.5)
    connector = aiohttp.TCPConnector(limit=40)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector,
                                     trust_env=False) as session:
        async def verified(host: str) -> bool:
            challenge = urlunsplit((parsed.scheme, f"{host}:{port}", "/identify", "", ""))
            try:
                async with session.get(challenge, params={"nonce": nonce},
                                       allow_redirects=False) as response:
                    if response.status != 200 or response.content_length and response.content_length > 1024:
                        return False
                    raw = await response.content.read(1025)
                    if len(raw) > 1024:
                        return False
                    body = json.loads(raw)
                    return (body.get("service") == "athena-pc-inbox"
                            and body.get("nonce") == nonce
                            and hmac.compare_digest(str(body.get("proof", "")), expected))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                return False

        if await verified(str(address)):
            return configured
        network = ipaddress.ip_network(f"{address}/24", strict=False)
        async def check(host: str):
            return host, await verified(host)
        tasks = [asyncio.create_task(check(str(host))) for host in network.hosts()
                 if host != address]
        found = None
        try:
            for completed in asyncio.as_completed(tasks):
                host, valid = await completed
                if valid:
                    found = host
                    break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if found:
            return urlunsplit((parsed.scheme, f"{found}:{port}", "/upload", "", ""))
    raise ConnectionError("The signed PC inbox is not reachable on this LAN.")
