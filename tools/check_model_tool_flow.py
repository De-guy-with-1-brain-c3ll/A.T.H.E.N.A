"""Silent live model routing checks; safe readers and a synthetic status fixture."""
import asyncio
import os
from pathlib import Path
import tempfile
from uuid import uuid4
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry
from athena.tools.clock import ClockTool
from athena.tools.weather import WeatherTool


async def main():
    load_local_environment()
    with tempfile.TemporaryDirectory() as directory:
        registry = ToolRegistry()
        registry.register(ClockTool()); registry.register(WeatherTool())
        fixture = registry.status_store.begin('coding_workspace', {'path': 'routing-test.py'})
        registry.status_store.finish(fixture, 'completed', 'Synthetic status fixture: file was checked.')
        questions = [
            ('How late is it here?', 'get_local_time', []),
            ('Will I need an umbrella in Shenzhen today?', 'get_weather', []),
            ('Is that file finished?', 'check_tool_status',
             [{'role': 'user', 'content': 'Check the routing-test.py coding file.'}]),
        ]
        for question, expected, context in questions:
            calls = []
            execute = registry.execute

            async def tracked(name, arguments, **kwargs):
                result = await execute(name, arguments, **kwargs)
                calls.append((name, result.success, arguments))
                return result

            registry.execute = tracked
            model = DeepSeekLanguageModel(os.environ['DEEPSEEK_API_KEY'],
                os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash'), registry,
                RuntimeSettingsStore(Path(directory)/'settings.json'))
            opened = model._open_stream
            count = 0

            async def bounded(request):
                nonlocal count
                count += 1
                if count > 5: raise RuntimeError('Five-request live check budget exhausted.')
                return await opened(request)

            model._open_stream = bounded
            try:
                answer = ''.join([part async for part in model.stream_reply(uuid4(), question, context)])
                print('Question:', question, flush=True)
                print('Answer:', answer, flush=True)
                print('Actual calls:', calls, '| Model requests:', count, flush=True)
                if expected == 'check_tool_status' and not calls:
                    if 'Synthetic status fixture' not in answer:
                        raise RuntimeError('The direct status receipt was not returned.')
                elif not any(name == expected for name, _, _ in calls):
                    raise RuntimeError('Expected tool was not called by the model: ' + expected)
            finally:
                registry.execute = execute
                await model._client.close()
        await registry.close()


if __name__ == '__main__': asyncio.run(main())
