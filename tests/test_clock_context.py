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
        self.assertIn("ground truth", message["content"])

    def test_the_clock_is_written_the_way_it_is_spoken(self):
        # This line is read aloud, and the model copies it verbatim when asked
        # the time. A bare %H:%M here came out as a bare "20:27" out loud, which
        # is the one thing you cannot say to someone who asked you the time.
        message = clock_message()["content"]
        self.assertRegex(message, r"at \d{1,2}:\d{2} [AP]M")
        self.assertNotRegex(message, r"at \d{2}:\d{2}\b(?! [AP]M)",
                            "no 24-hour clock survives anywhere in this line")

    def test_the_clock_says_which_part_of_the_day_it_is(self):
        # "8:27" alone is ambiguous out loud; the part of day disambiguates it.
        message = clock_message()["content"]
        self.assertTrue(
            any(part in message for part in
                ("in the morning", "in the afternoon", "in the evening")),
            "the clock must name the part of the day")

    def test_the_model_is_told_to_say_the_time_in_twelve_hour_form(self):
        message = clock_message()["content"]
        self.assertIn("12-hour", message)
        self.assertIn("never as 24-hour", message)

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
