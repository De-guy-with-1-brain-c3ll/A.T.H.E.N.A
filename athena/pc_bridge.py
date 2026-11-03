"""Signed requests to the configured private-LAN PC bridge."""
import hashlib
import asyncio
import ipaddress
import json
import os
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
import aiohttp
from athena.pc_transfer import signature
from athena.pc_discovery import resolve_pc_url


async def browser_request(body):
    return await pc_request('/browser', body)


async def pc_request(endpoint, body, timeout=35):
    # Bound discovery as well as HTTP; neither should leave a voice turn waiting.
    async with asyncio.timeout(timeout):
        return await _pc_request(endpoint, body, timeout)


async def _pc_request(endpoint, body, timeout):
    if endpoint not in {'/browser', '/desktop', '/openclaw'}:
        raise ValueError('Unsupported PC bridge endpoint.')
    configured = urlsplit(os.environ.get("ATHENA_PC_UPLOAD_URL", ""))
    key = os.environ.get("ATHENA_PC_TRANSFER_KEY", "")
    if len(key) < 32 or configured.scheme not in {"http", "https"} or configured.username:
        raise ValueError("Configure the PC bridge first.")
    if not ipaddress.ip_address(configured.hostname).is_private:
        raise ValueError("PC bridge must be on the private LAN.")
    resolved = urlsplit(await resolve_pc_url(configured.geturl(), key))
    url = urlunsplit((resolved.scheme, resolved.netloc, endpoint, "", ""))
    payload = json.dumps(body).encode()
    stamp, nonce = str(int(time.time())), uuid4().hex
    digest = hashlib.sha256(payload).hexdigest()
    headers = {"X-Athena-Time": stamp, "X-Athena-Nonce": nonce,
               "X-Athena-Name": "browser.json", "X-Athena-SHA256": digest,
               "X-Athena-Signature": signature(key, stamp, nonce, f"POST:{endpoint}:browser.json", digest)}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False) as session:
        async with session.post(url, data=payload, headers=headers, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError((await response.text())[:300])
            if response.content_length and response.content_length > 3_000_000:
                raise ValueError("PC response too large.")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 3_000_000: raise ValueError("PC response too large.")
                chunks.append(chunk)
            raw = b"".join(chunks)
            return json.loads(raw)
