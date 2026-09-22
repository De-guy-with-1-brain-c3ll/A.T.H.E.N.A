"""The model must always know the current local date and time.

"Every turn" is the contract: the clock is rebuilt per request in stream_reply,
never cached with the system prompt, so a process that has run for days cannot
answer from a stale morning.
"""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

from athena.llm.deepseek import DeepSeekLanguageModel, clock_message
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry


def chunk(text=None):
    return NS(choices=[NS(delta=NS(content=text, tool_calls=None))])


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for item in self.chunks:
            yield item


class ClockMessageTests(unittest.TestCase):
    def test_the_message_names_today_weekday_date_and_time(self):
        message = clock_message()
        self.assertEqual(message["role"], "system")
        now = datetime.now().astimezone()
        self.assertIn(now.strftime("%A"), message["content"])
        self.assertIn(now.strftime("%d %B %Y"), message["content"])
        self.assertRegex(message["content"], r"at \d{2}:\d{2}")
        self.assertIn("ground truth", message["content"])

    def test_two_calls_are_built_independently(self):
        # The point of the function: nothing is cached, so a request made days
        # later carries that day, not the day the process started.
        first, second = clock_message(), clock_message()
        self.assertIsNot(first, second)


class ClockReachesTheModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_reply_sends_the_clock_as_a_system_message(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry.discover(services={"settings": None})
            model = DeepSeekLanguageModel('test', 'test', registry,
                RuntimeSettingsStore(Path(directory) / 'settings.json'))
            model._client.chat.completions.create = AsyncMock(
                side_effect=[Stream([chunk('At your service.')])])
            try:
                answer = ''.join([part async for part in
                                  model.stream_reply(uuid4(), 'hello there')])
                self.assertIn('service', answer)
                sent = model._client.chat.completions.create.await_args.kwargs['messages']
                clocks = [item for item in sent if item.get("role") == "system"
                          and item.get("content", "").startswith(
                              "The current local date and time is")]
                self.assertEqual(len(clocks), 1,
                                 "exactly one clock message per request")
                self.assertTrue(sent[0]["content"], "the system prompt leads")
            finally:
                await model.close()


if __name__ == "__main__":
    unittest.main()
