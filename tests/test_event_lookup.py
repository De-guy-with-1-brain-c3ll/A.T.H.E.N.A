import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

from athena.llm.deepseek import DeepSeekLanguageModel, event_lookup_required
from athena.settings.store import RuntimeSettingsStore
from athena.tools.models import ToolDefinition, ToolResult
from athena.tools.registry import ToolRegistry
from tests.test_search_refinement import Stream, chunk, call


class EventRoutingTests(unittest.TestCase):
    def test_race_queries_and_short_corrections_keep_topic(self):
        context = [{"role": "user", "content": "2026 Bahrain Grand Prix, bro."},
                   {"role": "assistant", "content": "March 24. Want more details?"}]
        for text in ("Bahrain Grand Prix", "When's the Malaysia Grand Prix starting?",
                     "today.", "No, it is October fourth.", "Search online, please.",
                     "Where did you get that from?"):
            with self.subTest(text=text):
                self.assertTrue(event_lookup_required(text, context))

    def test_no_paid_lookup_for_greeting_or_unrelated_request(self):
        context = [{"role": "user", "content": "Bahrain Grand Prix"}]
        for text in ("hello", "what's the weather", "play some music", "thanks"):
            self.assertFalse(event_lookup_required(text, context))
        self.assertFalse(event_lookup_required("today", []))
        self.assertFalse(event_lookup_required("Explain F1 rules", []))
        self.assertFalse(event_lookup_required("today", [{"role": "assistant", "content": "Bahrain GP"}]))


class EventVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def run_lookup(self, streams, read_success=True):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry()
            for name, result in (
                ("search_web", ToolResult(True, "Sources found", {"results": [{"url": "https://www.formula1.com/en/racing/2026"}]})),
                ("read_webpage", ToolResult(read_success, "Page", {"text": "Official updated race: October 4, 2026 at 1500 MYT."} if read_success else {}))):
                registry.register(NS(definition=ToolDefinition(name, name, {"type": "object", "properties": {}}),
                                     execute=AsyncMock(return_value=result)))
            model = DeepSeekLanguageModel("test", "test", registry, RuntimeSettingsStore(Path(directory)/"settings.json"))
            model._client.chat.completions.create = AsyncMock(side_effect=streams)
            try:
                answer = "".join([part async for part in model.stream_reply(uuid4(), "today.",
                    [{"role": "user", "content": "Bahrain Grand Prix 2026"},
                     {"role": "assistant", "content": "March 24. Want more details?"}])])
                requests = [item.kwargs for item in model._client.chat.completions.create.await_args_list]
                return answer, requests
            finally:
                await model.close()

    async def test_lookup_and_source_read_are_required_before_date_answer(self):
        answer, requests = await self.run_lookup([
            call("search_web", {"query": "Bahrain GP 2026 October 4 official schedule"}, "search"),
            call("read_webpage", {"url": "https://www.formula1.com/en/racing/2026"}, "read"),
            Stream([chunk("Today, October 4, at 3 PM your time.")])])
        self.assertIn("October 4", answer)
        self.assertEqual(requests[0]["tool_choice"]["function"]["name"], "search_web")
        self.assertEqual(requests[1]["tool_choice"]["function"]["name"], "read_webpage")
        policy = " ".join(m["content"] for m in requests[0]["messages"] if m["role"] == "system")
        self.assertIn("Earlier assistant dates are unverified", policy)
        self.assertIn("Do not ask whether", policy)

    async def test_unsupported_stale_date_is_not_spoken(self):
        answer, _ = await self.run_lookup([Stream([chunk("The race is March 24. Want more details?")])])
        self.assertNotIn("March 24", answer)
        self.assertIn("couldn't verify", answer)
