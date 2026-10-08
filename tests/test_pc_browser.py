import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import socket
from uuid import uuid4
from aiohttp.test_utils import TestClient, TestServer
from athena.pc_browser import PCBrowser
from athena.pc_transfer import inbox_app, signature


class BrowserControlTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(hasattr(os, 'startfile'), 'os.startfile is Windows-only')
    async def test_windows_open_falls_back_when_playwright_cannot_start(self):
        browser = PCBrowser('unused-test-profile')
        browser.ensure = AsyncMock(side_effect=TimeoutError('driver unavailable'))
        with patch('athena.pc_browser.os.startfile') as start, patch.object(
                socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM,
                                                      6, '', ('93.184.215.14', 443))]):
            result = await browser.execute({'action': 'open', 'url': 'https://example.com/'})
        self.assertTrue(result['opened_external'])
        self.assertEqual(result['navigation_state'], 'launched_external')
        start.assert_called_once_with('https://example.com/')

    async def test_status_and_disable_do_not_wait_for_navigation(self):
        browser = PCBrowser("unused-test-profile")
        browser.navigation_state = "opening"
        browser.target_url = "https://google.com/"
        async with browser.lock:
            status = await asyncio.wait_for(browser.execute({"action": "status"}), .1)
            self.assertEqual(status["navigation_state"], "opening")
            await asyncio.wait_for(browser.execute({"action": "keyboard_config", "enabled": False}), .1)

    async def test_navigation_failure_is_a_site_error_not_missing_browser(self):
        browser = PCBrowser("unused-test-profile")
        browser.ensure = AsyncMock()
        browser.page = MagicMock()
        browser.page.goto = AsyncMock(side_effect=TimeoutError("site timeout"))
        with self.assertRaisesRegex(ValueError, "Could not load https://google.com/"):
            await browser.execute({"action": "open", "url": "https://google.com/"})
        status = await browser.execute({"action": "status"})
        self.assertEqual(status["navigation_state"], "failed")

    def test_private_and_nonweb_urls_rejected(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "http://localhost/", "http://127.0.0.1/", "http://192.168.33.153/", "https://user:password@example.com/"):
            with self.assertRaises(ValueError): PCBrowser.check_url(url)
        PCBrowser.check_url("https://example.com/")

    async def test_keyboard_requires_console_setting_and_never_launches_when_disabled(self):
        browser = PCBrowser("unused-test-profile")
        browser.ensure = AsyncMock()
        for action in ("type", "press", "click"):
            with self.assertRaisesRegex(ValueError, "disabled"):
                await browser.execute({"action": action, "text": "Hello", "key": "Enter", "x": 1, "y": 1})
        browser.ensure.assert_not_awaited()
        self.assertFalse((await browser.execute({"action": "status"}))["keyboard_enabled"])

    async def test_enabled_keyboard_is_confined_to_browser(self):
        browser = PCBrowser("unused-test-profile")
        browser.page = MagicMock()
        browser.page.is_closed.return_value = False
        browser.page.keyboard.insert_text = AsyncMock()
        browser.page.keyboard.press = AsyncMock()
        browser.page.title = AsyncMock(return_value="Test")
        browser.page.url = "https://example.com/"
        browser.ensure = AsyncMock()
        await browser.execute({"action": "keyboard_config", "enabled": True})
        await browser.execute({"action": "type", "text": "Hello"})
        browser.page.keyboard.insert_text.assert_awaited_once_with("Hello")
        with self.assertRaises(ValueError): await browser.execute({"action": "press", "key": "Meta+R"})
        await browser.execute({"action": "keyboard_config", "enabled": False})
        with self.assertRaises(ValueError): await browser.execute({"action": "type", "text": "Hello"})

    async def test_browser_auth_binds_endpoint_and_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            key = "test"*16
            browser = MagicMock(); browser.execute = AsyncMock(return_value={"keyboard_enabled": False})
            browser.close = AsyncMock()
            async with TestClient(TestServer(inbox_app(Path(directory)/"inbox", key, browser))) as client:
                body = json.dumps({"action": "status"}).encode()
                stamp, nonce, digest = str(int(time.time())), uuid4().hex, hashlib.sha256(body).hexdigest()
                headers = {"X-Athena-Time": stamp, "X-Athena-Nonce": nonce, "X-Athena-Name": "browser.json",
                           "X-Athena-SHA256": digest, "X-Athena-Signature": signature(key, stamp, nonce, "browser.json", digest)}
                self.assertEqual((await client.post("/browser", data=body, headers=headers)).status, 403)
                headers["X-Athena-Signature"] = signature(key, stamp, nonce, "POST:/browser:browser.json", digest)
                response = await client.post("/browser", data=body, headers=headers)
                self.assertEqual(response.status, 200)
                browser.execute.assert_awaited_once_with({"action": "status"})
                self.assertEqual((await client.post("/browser", data=body, headers=headers)).status, 403)
