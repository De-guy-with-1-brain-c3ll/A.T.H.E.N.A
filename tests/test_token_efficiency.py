import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from athena.llm.deepseek import DeepSeekLanguageModel
from athena.memory.service import MemoryService
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry


class TokenEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_intent_router_omits_irrelevant_tool_schemas(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = RuntimeSettingsStore(Path(directory) / 'settings.json')
            registry = ToolRegistry.discover(services={'settings': settings})
            model = DeepSeekLanguageModel('test', 'test', registry, settings)
            self.assertEqual(model._tool_names_for('hello'), set())
            self.assertEqual(model._tool_names_for('what tools do you have'), set())
            self.assertEqual(model._tool_names_for('weather in Shenzhen'), {'get_weather'})
            self.assertEqual(model._tool_names_for('what time is it'), {'get_local_time'})
            self.assertIn('coding_workspace', model._tool_names_for('write and test a Python program'))
            download = model._tool_names_for('download the latest installer')
            self.assertIn('download_file', download)
            self.assertNotIn('coding_workspace', download)
            self.assertLess(len(registry.definitions(download)), len(registry.definitions()))
            await model.close()

    async def test_capability_question_is_answered_without_model_tokens(self):
        registry = ToolRegistry.discover()
        result = await registry.handle_user_command('what tools do you have')
        self.assertTrue(result.success)
        self.assertIn('weather', result.spoken_text)
        self.assertIn('approved downloads', result.spoken_text)

    async def test_memory_summary_uses_one_model_call_per_six_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryService(Path(directory) / 'memory.db', 'test', 'test',
                                   batch_delay_seconds=0.01)
            await memory.connect()
            memory._concentrate = AsyncMock()
            try:
                for index in range(5):
                    await memory.remember_turn(uuid4(), f'user {index}', f'answer {index}')
                await asyncio.wait_for(memory._queue.join(), 2)
                memory._concentrate.assert_not_awaited()
                await memory.remember_turn(uuid4(), 'user 5', 'answer 5')
                await asyncio.wait_for(memory._queue.join(), 2)
                memory._concentrate.assert_awaited_once()
                self.assertEqual(len(memory._concentrate.await_args.args[0]), 6)
            finally:
                await memory.close()

    async def test_api_keys_are_not_saved_or_returned_to_model_context(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryService(Path(directory) / 'memory.db', 'test', 'test',
                                   batch_delay_seconds=0.01)
            await memory.connect()
            try:
                await memory.remember_turn(uuid4(), 'my key is sk-abcdefghijklmnopqrstuvwxyz1234',
                                           'I will remember it')
                self.assertTrue(memory._queue.empty())
                self.assertEqual(memory.context_messages(), [])
            finally:
                await memory.close()
