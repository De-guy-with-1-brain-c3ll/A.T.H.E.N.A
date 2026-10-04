"""A dedicated PC browser, not unrestricted desktop or shell control."""
from __future__ import annotations
import asyncio
import base64
import ipaddress
import socket
import os
from pathlib import Path
from urllib.parse import urlsplit


class PCBrowser:
    def __init__(self, profile):
        self.profile = Path(profile)
        self.keyboard_enabled = False
        self.lock = asyncio.Lock()
        self.playwright = self.context = self.page = None
        self.navigation_state = "idle"
        self.target_url = ""
        self.navigation_error = ""

    @staticmethod
    def check_url(url):
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only public HTTP/HTTPS pages are supported.")
        if parsed.hostname.lower() in {"localhost", "localhost.localdomain"} or parsed.hostname.endswith((".local", ".localhost")):
            raise ValueError("Local browser targets are blocked.")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return
        if not address.is_global:
            raise ValueError("Private and local browser targets are blocked.")

    async def ensure(self):
        if self.context is not None and self.page is not None and not self.page.is_closed():
            return
        if self.context is not None:
            await self.context.close()
        if self.playwright is None:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
        self.profile.mkdir(parents=True, exist_ok=True)
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.profile), headless=False, viewport={"width": 1280, "height": 720},
            channel=os.environ.get("ATHENA_PC_BROWSER_CHANNEL") or ("msedge" if os.name == "nt" else None),
            chromium_sandbox=os.name == "nt", accept_downloads=False, service_workers="block")
        # Block private-address navigation and page-initiated downloads. This is
        # a browser profile for user-requested public sites, not a LAN scanner.
        async def guard(route):
            try:
                self.check_url(route.request.url)
                host = urlsplit(route.request.url).hostname
                addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM), 3)
                if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                    raise ValueError("Private DNS target blocked.")
                await route.continue_()
            except (ValueError, OSError, TimeoutError):
                await route.abort()
        await self.context.route("**/*", guard)
        def adopt(page):
            self.page = page
            page.on("download", lambda download: asyncio.create_task(download.cancel()))
        self.context.on("page", adopt)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("download", lambda download: asyncio.create_task(download.cancel()))

    async def execute(self, body):
        # Status and the human's kill switch must not queue behind navigation.
        if body.get("action") == "status":
            return {"keyboard_enabled": self.keyboard_enabled,
                    "url": self.page.url if self.page and not self.page.is_closed() else "",
                    "running": bool(self.page and not self.page.is_closed()),
                    "navigation_state": self.navigation_state, "target_url": self.target_url,
                    "navigation_error": self.navigation_error}
        if body.get("action") == "keyboard_config":
            if type(body.get("enabled")) is not bool:
                raise ValueError("Keyboard control must be true or false.")
            self.keyboard_enabled = body["enabled"]
            return {"keyboard_enabled": self.keyboard_enabled}
        async with self.lock:
            action = body.get("action")
            if action == "status":
                return {"keyboard_enabled": self.keyboard_enabled,
                        "url": self.page.url if self.page and not self.page.is_closed() else "",
                        "running": bool(self.page and not self.page.is_closed())}
            if action == "keyboard_config":
                if type(body.get("enabled")) is not bool:
                    raise ValueError("Keyboard control must be true or false.")
                self.keyboard_enabled = body["enabled"]
                return {"keyboard_enabled": self.keyboard_enabled}
            if action not in {"open", "screenshot", "type", "press", "click"}:
                raise ValueError("Unsupported PC browser action.")
            if action in {"type", "press", "click"} and not self.keyboard_enabled:
                raise ValueError("PC keyboard/mouse control is disabled. Enable it in the console first.")
            if action == "open":
                self.check_url(str(body.get("url", "")))
            elif self.page is None or self.page.is_closed():
                raise ValueError("Open a browser page first.")
            else:
                self.check_url(self.page.url)
            if action == "open":
                self.target_url = body["url"]
                self.navigation_state, self.navigation_error = "opening", ""
            try:
                await asyncio.wait_for(self.ensure(), 10)
            except Exception as error:
                self.navigation_state = "failed"
                self.navigation_error = f"PC browser could not start: {str(error)[:200] or type(error).__name__}"
                raise ValueError(self.navigation_error) from error
            if action == "open":
                try:
                    await self.page.goto(body["url"], wait_until="domcontentloaded", timeout=8000)
                except Exception as error:
                    self.navigation_state = "failed"
                    self.navigation_error = f"Could not load {self.target_url}: {str(error).splitlines()[0][:200]}"
                    raise ValueError(self.navigation_error) from error
                self.navigation_state = "loaded"
                await self.page.bring_to_front()
            elif action == "type":
                text = body.get("text")
                if not isinstance(text, str) or len(text) > 2000:
                    raise ValueError("Typing is limited to 2,000 characters.")
                await self.page.keyboard.insert_text(text)
            elif action == "press":
                key = body.get("key")
                if key not in {"Enter", "Tab", "Escape", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Control+A"}:
                    raise ValueError("Unsupported browser key.")
                await self.page.keyboard.press(key)
            elif action == "click":
                x, y = body.get("x"), body.get("y")
                if type(x) is not int or type(y) is not int or not (0 <= x < 1280 and 0 <= y < 720):
                    raise ValueError("Click coordinates must be inside the 1280×720 browser viewport.")
                await self.page.mouse.click(x, y)
            result = {"url": self.page.url, "title": (await self.page.title())[:250],
                      "keyboard_enabled": self.keyboard_enabled}
            if action == "screenshot":
                image = await self.page.screenshot(type="jpeg", quality=65, full_page=False, timeout=10000)
                if len(image) > 2_000_000:
                    raise ValueError("Screenshot is too large.")
                result.update(image=base64.b64encode(image).decode(), mime="image/jpeg", width=1280, height=720)
            return result

    async def close(self):
        if self.context: await self.context.close()
        if self.playwright: await self.playwright.stop()
