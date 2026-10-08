import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

from athena.llm.deepseek import DeepSeekLanguageModel, lookup_permitted, missing_public_information
from athena.settings.store import RuntimeSettingsStore
from athena.tools.models import ToolDefinition, ToolResult
from athena.tools.registry import ToolRegistry
from tests.test_search_refinement import Stream, chunk, call


class AutomaticLookupTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, text, streams, names=("search_web", "read_webpage")):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry()
            tools = {}
            for name in names:
                tool = NS(definition=ToolDefinition(name, name, {"type": "object", "properties": {}}),
                    execute=AsyncMock(return_value=ToolResult(True, "Verified result", {"text": "Verified fact", "results": [{"url": "https://example.com"}]})))
                registry.register(tool); tools[name] = tool
            model = DeepSeekLanguageModel("test", "test", registry, RuntimeSettingsStore(Path(directory)/"settings.json"))
            model._client.chat.completions.create = AsyncMock(side_effect=streams)
            try:
                result = ''.join([part async for part in model.stream_reply(uuid4(), text)])
                requests = [c.kwargs for c in model._client.chat.completions.create.await_args_list]
                return result, requests, tools
            finally:
                await model.close()

    async def test_unfamiliar_and_recent_facts_are_searched_without_permission(self):
        for question, uncertainty in (
            ("Who won the 2026 Ig Nobel physics prize?", "I don't have that information. Would you like me to search?"),
            ("What is the latest firmware for the X96 Max?", "That is beyond my knowledge cutoff."),
            ("Tell me about the newly announced satellite mission.", "I'm not sure. Shall I look it up?")):
            with self.subTest(question=question):
                result, requests, tools = await self.run_case(question, [
                    Stream([chunk(uncertainty)]), call("search_web", {"query": question}, "search"),
                    call("read_webpage", {"url": "https://example.com"}, "read"),
                    Stream([chunk("Here is the verified fact.")])])
                self.assertNotIn(uncertainty, result)
                self.assertIn("I'll search online", result)
                self.assertIn("verified fact", result)
                self.assertEqual(requests[1]["tool_choice"]["function"]["name"], "search_web")
                tools["search_web"].execute.assert_awaited_once()

    async def test_dedicated_tool_offer_becomes_an_actual_call(self):
        result, requests, tools = await self.run_case("What's the weather in Shenzhen?", [
            Stream([chunk("Would you like me to check the weather?")]),
            call("get_weather", {"city": "Shenzhen"}, "weather"),
            Stream([chunk("It's 28 degrees in Shenzhen.")])], names=("get_weather",))
        self.assertNotIn("Would you like", result)
        self.assertNotIn("search online", result)
        tools["get_weather"].execute.assert_awaited_once()

    async def test_known_facts_and_greetings_do_not_search(self):
        for question, answer in (("What is photosynthesis?", "Plants turn light into chemical energy."), ("hello", "Hello, sir.")):
            result, requests, tools = await self.run_case(question, [Stream([chunk(answer)])])
            self.assertEqual(result, answer)
            self.assertEqual(len(requests), 1)
            tools["search_web"].execute.assert_not_awaited()

    async def test_explicit_offline_request_is_respected(self):
        result, requests, tools = await self.run_case("Who won? Do not search online.",
            [Stream([chunk("I don't know.")])])
        self.assertEqual(len(requests), 1)
        tools["search_web"].execute.assert_not_awaited()

    def test_private_information_is_not_a_public_search(self):
        for text in ("what's my password", "check my assignments", "my account", "without browsing"):
            self.assertFalse(lookup_permitted(text))
        self.assertTrue(missing_public_information("I don't know. Would you like me to search?"))
        self.assertFalse(missing_public_information("The official source confirms it."))
