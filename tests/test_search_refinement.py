import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.settings.store import RuntimeSettingsStore
from athena.tools.models import ToolDefinition, ToolResult
from athena.tools.registry import ToolRegistry
def chunk(text=None, calls=None):
    return NS(choices=[NS(delta=NS(content=text, tool_calls=calls))])

class Stream:
    def __init__(self, chunks): self.chunks = chunks
    async def __aiter__(self):
        for item in self.chunks: yield item

def call(name, arguments, identity):
    return Stream([chunk(calls=[NS(index=0, id=identity, function=NS(name=name, arguments=json.dumps(arguments)))])])

class SearchRefinementTests(unittest.IsolatedAsyncioTestCase):
    async def test_unusable_hits_are_refined_and_read_before_answering(self):
        with tempfile.TemporaryDirectory() as directory:
            search = NS(definition=ToolDefinition("search_web", "search", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
                execute=AsyncMock(side_effect=[ToolResult(True, "3 hits", {"results": [{"title": "unhelpful"}]}),
                    ToolResult(True, "usable article", {"results": [{"url": "https://example.com/article"}]})]))
            read = NS(definition=ToolDefinition("read_webpage", "read", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}),
                execute=AsyncMock(return_value=ToolResult(True, "article retrieved", {"text": "Verified event happened today."})))
            registry = ToolRegistry(); registry.register(search); registry.register(read)
            model = DeepSeekLanguageModel("test", "test", registry, RuntimeSettingsStore(Path(directory)/"settings.json"))
            model._client.chat.completions.create = AsyncMock(side_effect=[
                call("search_web", {"query": "news today"}, "first"),
                Stream([chunk("I found 3 results but none were usable.")]),
                call("search_web", {"query": "2026 Chinese news specific source"}, "refined"),
                call("read_webpage", {"url": "https://example.com/article"}, "read"),
                Stream([chunk("Here is the verified event from the article.")])])
            try:
                reply = "".join([part async for part in model.stream_reply(uuid4(), "search the news today")])
                self.assertNotIn("none were usable", reply)
                self.assertIn("verified event", reply)
                self.assertEqual(search.execute.await_count, 2)
                read.execute.assert_awaited_once()
            finally: await model.close()

    async def test_failed_searches_stop_after_three_distinct_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            search = NS(definition=ToolDefinition("search_web", "search", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
                execute=AsyncMock(return_value=ToolResult(False, "No usable sources", {})))
            registry = ToolRegistry(); registry.register(search)
            model = DeepSeekLanguageModel("test", "test", registry, RuntimeSettingsStore(Path(directory)/"settings.json"))
            streams = []
            for index in range(3):
                streams += [call("search_web", {"query": f"news query {index}"}, str(index)), Stream([chunk("No usable sources found.")])]
            model._client.chat.completions.create = AsyncMock(side_effect=streams)
            try:
                reply = "".join([part async for part in model.stream_reply(uuid4(), "search the news")])
                self.assertEqual(search.execute.await_count, 3)
                self.assertEqual(model._client.chat.completions.create.await_count, 6)
                self.assertIn("No usable sources", reply)
            finally: await model.close()
