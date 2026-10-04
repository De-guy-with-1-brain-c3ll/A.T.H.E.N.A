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

    async def test_empty_search_refines_once_without_extra_model_tokens(self):
        http = AsyncMock()
        empty = WebResponse("https://www.bing.com/search", b"<rss><channel/></rss>", "application/rss+xml")
        usable = WebResponse("https://www.bing.com/search", b'<rss><channel><item><title>Relevant article</title><link>https://example.com</link></item></channel></rss>', "application/rss+xml")
        http.get.side_effect = [empty, usable]
        result = await SearchWebTool(http).execute({"query": "please find me solar panels"})
        self.assertTrue(result.success)
        self.assertEqual(len(result.data["attempted_queries"]), 2)
        self.assertNotEqual(*result.data["attempted_queries"])
        self.assertEqual(http.get.await_count, 2)

    async def test_empty_search_has_a_hard_two_request_limit(self):
        http = AsyncMock()
        http.get.return_value = WebResponse("https://www.bing.com/search", b"<rss><channel/></rss>", "application/rss+xml")
        result = await SearchWebTool(http).execute({"query": "Chinese news today"})
        self.assertFalse(result.success)
        self.assertEqual(http.get.await_count, 2)
        self.assertTrue(result.data["chinese_news_filter"])

    async def test_chinese_news_filters_dictionary_matches_and_non_news_domains(self):
        http = AsyncMock()
        http.get.return_value = WebResponse(
            "https://www.bing.com/search",
            """<rss><channel>
              <item><title>today是什么意思</title><link>https://baike.baidu.com/item/today</link><description>translation</description></item>
              <item><title>China Daily News</title><link>https://news.qq.com/rain/2026-10-02</link><description>今日要闻</description></item>
            </channel></rss>""".encode("utf-8"),
            "application/rss+xml",
        )
        result = await SearchWebTool(http).execute({"query": "tell me today's news, use Chinese sources"})
        self.assertTrue(result.success)
        self.assertTrue(result.data["chinese_news_filter"])
        self.assertEqual([item["url"] for item in result.data["results"]],
                         ["https://news.qq.com/rain/2026-10-02"])
        self.assertIn("%E4%B8%AD%E5%9B%BD", http.get.call_args.args[0])

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
