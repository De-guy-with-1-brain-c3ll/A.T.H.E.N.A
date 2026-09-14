import unittest
from unittest.mock import AsyncMock

from athena.tools.registry import ToolRegistry
from athena.tools.models import ToolDefinition, ToolResult


class DiagnosticTool:
    def __init__(self, name, result):
        self.definition = ToolDefinition(name=name, description='test',
            parameters={"type": "object", "additionalProperties": True})
        self.execute = AsyncMock(return_value=result)


class ToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_and_executes_clock_tool(self):
        registry = ToolRegistry.discover()
        self.assertIn("get_local_time", registry.names())
        self.assertNotIn("replace_with_unique_name", registry.names())
        result = await registry.execute(
            "get_local_time", {"timezone": "UTC"}
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["timezone"], "UTC")

    async def test_browser_diagnostic_runs_browser_not_search(self):
        registry = ToolRegistry()
        browser = DiagnosticTool('browse_webpage', ToolResult(True, 'read', {'text': 'Example Domain'}))
        search = DiagnosticTool('search_web', ToolResult(True, 'search', {'results': [{}]}))
        registry.register(browser)
        registry.register(search)
        result = await registry.handle_user_command('go ahead and test the internet browsing tool')
        self.assertTrue(result.success)
        browser.execute.assert_awaited_once()
        search.execute.assert_not_awaited()
        self.assertEqual(result.data['diagnostic'], 'browse_webpage')

        browser.execute.reset_mock()
        result = await registry.handle_user_command('is the web browsing working?')
        self.assertTrue(result.success)
        browser.execute.assert_awaited_once()

    async def test_search_diagnostic_reports_actual_result_count(self):
        registry = ToolRegistry()
        search = DiagnosticTool('search_web', ToolResult(True, 'search', {'results': [{}, {}, {}]}))
        registry.register(search)
        result = await registry.handle_user_command('test the web search tool')
        self.assertTrue(result.success)
        self.assertIn('3 usable results', result.spoken_text)
        search.execute.assert_awaited_once()

        search.execute.reset_mock()
        result = await registry.handle_user_command('try to use your web search tool')
        self.assertTrue(result.success)
        self.assertIn('3 usable results', result.spoken_text)
        search.execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
