"""Bounded public GETs. DNS is validated inside the actual network connector."""
from __future__ import annotations
from dataclasses import dataclass
import asyncio
import hashlib
import ipaddress
import json
import socket
from urllib.parse import urljoin, urlsplit
import aiohttp
from aiohttp.resolver import ThreadedResolver


class PublicWebError(ValueError):
    pass


class DownloadRedirect(PublicWebError):
    def __init__(self, url):
        self.url = url
        super().__init__("The download destination changed. Prepare the new destination for approval.")


def public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def validate_url(url: str) -> str:
    if len(url) > 4096 or any(ord(c) < 33 for c in url) or "\\" in url:
        raise PublicWebError("Invalid URL characters or length.")
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise PublicWebError("Only public HTTP and HTTPS URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise PublicWebError("Credentials in URLs are not allowed.")
    if parsed.port not in (None, 80, 443):
        raise PublicWebError("Only standard web ports are allowed.")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise PublicWebError("Local network addresses are not allowed.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # Hostname checked by PublicResolver when connecting.
    else:
        if not public_ip(host):
            raise PublicWebError("Private and reserved addresses are not allowed.")
    return url


class PublicResolver(ThreadedResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await super().resolve(host, port, family)
        if not results or any(not public_ip(item["host"]) for item in results):
            raise PublicWebError("DNS resolved to a private or reserved address.")
        return results


@dataclass(frozen=True)
class WebResponse:
    url: str
    body: bytes
    content_type: str
    charset: str = "utf-8"

    def text(self) -> str:
        try:
            return self.body.decode(self.charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class PublicHTTP:
    def __init__(self, max_bytes: int = 2_000_000, timeout: float = 15):
        self.max_bytes, self.timeout = max_bytes, timeout

    async def inspect_download(self, url: str) -> dict:
        """Resolve public HTTPS redirects using HEAD only; never fetch a file body."""
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=1)
        chain = []
        async with asyncio.timeout(25), aiohttp.ClientSession(
            connector=connector, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=25),
            headers={"User-Agent": "ATHENA/0.1 (download metadata)", "Accept-Encoding": "identity"},
        ) as session:
            for _ in range(10):
                validate_url(url)
                if urlsplit(url).scheme != "https":
                    raise PublicWebError("Download redirects must stay on public HTTPS.")
                chain.append(url)
                async with session.head(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if not response.headers.get("Location"):
                            raise PublicWebError("Redirect has no destination.")
                        url = urljoin(str(response.url), response.headers["Location"])
                        continue
                    if response.status != 200:
                        raise PublicWebError(f"Could not inspect the file (HTTP {response.status}). No download started.")
                    return {"url": str(response.url), "bytes": response.content_length,
                            "content_type": response.content_type,
                            "content_disposition": response.headers.get("Content-Disposition", ""),
                            "redirect_chain": chain}
            raise PublicWebError("Too many download redirects.")

    async def download(self, url: str, destination, progress=None) -> dict:
        """Stream to an already-open staging file, within the approved origin."""
        validate_url(url)
        def origin(value):
            parsed = urlsplit(value)
            return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        approved_origin = origin(url)
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=1)
        async with asyncio.timeout(self.timeout), aiohttp.ClientSession(
            connector=connector, trust_env=False, auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            headers={"User-Agent": "ATHENA/0.1 (approved download)", "Accept-Encoding": "identity"},
        ) as session:
            for _ in range(5):
                validate_url(url)
                if origin(url) != approved_origin:
                    raise DownloadRedirect(url)
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if not response.headers.get("Location"):
                            raise PublicWebError("Redirect has no destination.")
                        url = urljoin(str(response.url), response.headers["Location"])
                        continue
                    if response.status != 200:
                        raise PublicWebError(f"Download returned HTTP {response.status}.")
                    if response.content_type in {"text/html", "application/xhtml+xml"}:
                        raise PublicWebError("The server returned a webpage instead of the file. No webpage was saved.")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise PublicWebError("Unexpected transfer encoding; use a direct file URL.")
                    if response.content_length is not None and response.content_length > self.max_bytes:
                        raise PublicWebError(f"Download exceeds the {self.max_bytes // (1024*1024)} MiB limit.")
                    size, checksum = 0, hashlib.sha256()
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise PublicWebError(f"Download exceeds the {self.max_bytes // (1024*1024)} MiB limit.")
                        destination.write(chunk)
                        checksum.update(chunk)
                        if progress is not None:
                            progress(size, response.content_length)
                    return {"url": str(response.url), "bytes": size, "sha256": checksum.hexdigest(),
                            "content_type": response.content_type}
            raise PublicWebError("Too many download redirects.")

    async def get(self, url: str) -> WebResponse:
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=8)
        async with aiohttp.ClientSession(
            connector=connector, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            headers={"User-Agent": "ATHENA/0.1 (personal read-only assistant)"},
        ) as session:
            for _ in range(5):
                validate_url(url)
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise PublicWebError("Redirect has no destination.")
                        url = urljoin(str(response.url), location)
                        continue
                    if response.status >= 400:
                        raise PublicWebError(f"Website returned HTTP {response.status}.")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(32768):
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise PublicWebError("Download size limit exceeded.")
                    return WebResponse(str(response.url), bytes(data),
                                       response.content_type, response.charset or "utf-8")
            raise PublicWebError("Too many redirects.")

    async def json(self, url: str) -> dict:
        response = await self.get(url)
        try:
            value = json.loads(response.body)
        except (ValueError, UnicodeError):
            raise PublicWebError("Provider returned invalid JSON.") from None
        if not isinstance(value, dict):
            raise PublicWebError("Provider returned an unexpected response.")
        return value
