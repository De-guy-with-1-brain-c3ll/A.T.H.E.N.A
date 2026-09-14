import os
import unittest
from unittest.mock import AsyncMock
from athena.tools._http import WebResponse
from athena.tools.web import BrowseWebpageTool


@unittest.skipUnless(os.environ.get("ATHENA_TEST_BROWSER") == "1", "Set ATHENA_TEST_BROWSER=1 to launch Chromium")
class BrowserIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_renders_javascript_without_external_network(self):
        http = AsyncMock()
        http.get.return_value = WebResponse("https://athena.test/", b'<html><title>Browser Test</title><body><main id="result">Initial</main><script>document.getElementById("result").textContent="JavaScript rendered successfully";</script></body></html>', "text/html")
        result = await BrowseWebpageTool(http).execute({"url": "https://athena.test/"})
        self.assertTrue(result.success, result.spoken_text)
        self.assertIn("JavaScript rendered successfully", result.data["text"])
        self.assertTrue(result.data["rendered"])
