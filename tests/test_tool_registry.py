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

    async def test_every_interface_gets_a_shared_sleep_record(self):
        """Same failure the alarms had: a tool that works in one interface only.

        Built through `build_registry`, both sleep tools must be discovered and
        must already have their shared state, or `sleep_status` reports "never"
        on a machine that consolidates nightly.
        """
        import tempfile
        from pathlib import Path

        from athena.services import build_registry
        from athena.settings.store import RuntimeSettingsStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            settings = RuntimeSettingsStore(Path(directory) / "settings.json")
            registry, _alerts = build_registry(settings)
            self.assertIn("sleep_mode", registry.names())
            self.assertIn("sleep_status", registry.names())
            self.assertIsNotNone(registry.get("sleep_mode").runner)
            self.assertIsNotNone(registry.get("sleep_status").store)

    async def test_the_sleep_status_tool_answers_through_the_registry(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from athena.services import build_registry
        from athena.settings.store import RuntimeSettingsStore
        from athena.sleep import SleepStatusStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            # Point the status file somewhere empty so the answer is deterministic.
            empty = SleepStatusStore(Path(directory) / "sleep-status.json")
            with patch("athena.tools.sleep.SleepStatusStore", lambda *a, **k: empty):
                settings = RuntimeSettingsStore(Path(directory) / "settings.json")
                registry, _alerts = build_registry(settings)
                result = await registry.execute("sleep_status", {})
            self.assertTrue(result.success)
            self.assertIn("not consolidated", result.spoken_text)

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
