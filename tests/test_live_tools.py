"""Opt-in public network tests, plus a separately enabled billed DeepSeek test."""
import os
import unittest
import tempfile
from pathlib import Path
from uuid import uuid4
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry
from athena.tools.weather import WeatherTool
from athena.tools.web import ReadWebpageTool, SearchWebTool, BrowseWebpageTool


@unittest.skipUnless(os.environ.get('ATHENA_TEST_NETWORK') == '1', 'Opt-in live network test')
class LiveToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_weather(self):
        result = await WeatherTool().execute({'location': '31.23,121.47', 'days': 1})
        self.assertTrue(result.success, result.spoken_text)
        self.assertIn('temperature_2m', result.data['current'])

    async def test_live_reader(self):
        result = await ReadWebpageTool().execute({'url': 'https://example.com/'})
        self.assertTrue(result.success, result.spoken_text)
        self.assertIn('Example Domain', result.data['text'])

    async def test_live_search(self):
        result = await SearchWebTool().execute({'query': 'Python official documentation', 'limit': 3})
        self.assertTrue(result.success, result.spoken_text)
        self.assertTrue(result.data['results'])

    async def test_live_browser(self):
        result = await BrowseWebpageTool().execute({'url': 'https://example.com/'})
        self.assertTrue(result.success, result.spoken_text)
        self.assertIn('Example Domain', result.data['text'])


@unittest.skipUnless(os.environ.get('ATHENA_TEST_DEEPSEEK') == '1', 'Opt-in billed DeepSeek test')
class LiveDeepSeekTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_selects_weather_and_uses_result(self):
        load_local_environment()
        key = os.environ.get('DEEPSEEK_API_KEY')
        if not key:
            self.skipTest('DEEPSEEK_API_KEY is not configured')
        registry = ToolRegistry.discover()
        original = registry.execute
        results = []
        async def execute(name, arguments, **kwargs):
            result = await original(name, arguments, **kwargs)
            results.append((name, result.success))
            return result
        registry.execute = execute
        with tempfile.TemporaryDirectory() as directory:
            settings = RuntimeSettingsStore(Path(directory) / 'settings.json')
            model = DeepSeekLanguageModel(key, os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash'), registry, settings)
            try:
                answer = ''.join([part async for part in model.stream_reply(uuid4(),
                    'Use get_weather to check current temperature at 31.23,121.47. Give one short sentence. Do not change settings or create files.')])
            finally:
                await model.close()
        self.assertIn(('get_weather', True), results)
        self.assertTrue(answer.strip())
