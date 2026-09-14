import socket
import unittest
from unittest.mock import AsyncMock, patch

from athena.tools._http import PublicResolver, PublicWebError, WebResponse, public_ip, validate_url
from athena.tools.web import ReadWebpageTool, SearchWebTool, extract_page
from athena.tools.registry import ToolRegistry


class PublicAddressTests(unittest.IsolatedAsyncioTestCase):
    def test_rejects_private_schemes_ports_and_credentials(self):
        for url in ["file:///etc/passwd", "http://localhost", "http://127.0.0.1",
                    "http://10.2.3.4", "http://169.254.169.254", "http://[::1]",
                    "http://[::ffff:127.0.0.1]", "http://192.168.0.5", "https://example.com:8080",
                    "https://user:secret@example.com", "http://x.local", "https://example.com/\n"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        self.assertEqual(validate_url("https://example.com/page"), "https://example.com/page")
        self.assertFalse(public_ip("224.0.0.1"))

    async def test_connector_rejects_private_or_mixed_dns(self):
        for answers in [[{"host": "127.0.0.1"}], [{"host": "1.1.1.1"}, {"host": "192.168.1.3"}], []]:
            with patch("aiohttp.resolver.ThreadedResolver.resolve", AsyncMock(return_value=answers)):
                with self.assertRaises(PublicWebError):
                    await PublicResolver().resolve("example.com", 443, socket.AF_INET)

    async def test_connector_uses_validated_public_answers(self):
        answers = [{"host": "1.1.1.1"}]
        with patch("aiohttp.resolver.ThreadedResolver.resolve", AsyncMock(return_value=answers)):
            self.assertEqual(await PublicResolver().resolve("example.com"), answers)


class WebToolTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_article_strips_noise_and_limits_content(self):
        html = '<title>News</title><nav>menu</nav><main><h1>Hello</h1><p>' + 'word ' * 500 + '</p><a href="/next">Next</a><script>secret()</script></main>'
        result = extract_page(html, "https://example.com", 100)
        self.assertEqual(result["title"], "News")
        self.assertTrue(result["truncated"])
        self.assertNotIn("secret", result["text"])
        self.assertNotIn("menu", result["text"])
        self.assertEqual(result["links"][0]["url"], "https://example.com/next")
        self.assertTrue(result["untrusted_content"])

    async def test_reader_handles_html_text_and_binary(self):
        http = AsyncMock()
        http.get.return_value = WebResponse("https://example.com", b"<main>Actual content</main>", "text/html")
        tool = ReadWebpageTool(http)
        result = await tool.execute({"url": "https://example.com"})
        self.assertTrue(result.success)
        self.assertEqual(result.data["text"], "Actual content")
        http.get.return_value = WebResponse("https://example.com", b"pdf", "application/pdf")
        self.assertFalse((await tool.execute({"url": "https://example.com"})).success)

    async def test_search_returns_sources_and_rejects_entities(self):
        http = AsyncMock()
        http.get.return_value = WebResponse("https://www.bing.com/search", b'<rss><channel><item><title>Result</title><link>https://example.com</link><description>Snippet</description></item></channel></rss>', "application/rss+xml")
        tool = SearchWebTool(http)
        result = await tool.execute({"query": "test"})
        self.assertTrue(result.success)
        self.assertEqual(result.data["results"][0]["url"], "https://example.com")
        http.get.return_value = WebResponse("https://example.com", b'<!DOCTYPE rss><rss/>', "application/xml")
        self.assertFalse((await tool.execute({"query": "test"})).success)

    async def test_network_failure_does_not_fabricate_content(self):
        tool = ReadWebpageTool(AsyncMock(get=AsyncMock(side_effect=TimeoutError)))
        self.assertFalse((await tool.execute({"url": "https://example.com"})).success)

    async def test_registry_discovers_all_tools_and_enforces_types(self):
        registry = ToolRegistry.discover()
        for name in ["get_weather", "search_web", "read_webpage", "browse_webpage", "coding_workspace"]:
            self.assertIn(name, registry.names())
        for args in [{"location": 123}, {"location": "Shanghai", "days": 99},
                     {"location": "Shanghai", "extra": "x"}, {"location": "Shanghai", "units": "kelvin"}]:
            with self.assertRaises(ValueError):
                await registry.execute("get_weather", args)
