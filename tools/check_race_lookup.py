"""One bounded live race lookup on the Pi; no speaker or conversation writes."""
import asyncio
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from athena.llm.deepseek import DeepSeekLanguageModel
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry
from athena.tools.web import SearchWebTool, ReadWebpageTool


async def main():
    for line in Path('/etc/athena/athena.env').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))
    registry = ToolRegistry()
    registry.register(SearchWebTool())
    registry.register(ReadWebpageTool())
    with tempfile.TemporaryDirectory() as directory:
        model = DeepSeekLanguageModel(os.environ['DEEPSEEK_API_KEY'],
            os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash'), registry,
            RuntimeSettingsStore(Path(directory)/'settings.json'))
        opened = model._open_stream
        requests = 0

        async def bounded(request):
            nonlocal requests
            requests += 1
            if requests > 4:
                raise RuntimeError('Live check stopped at four model requests.')
            return await opened(request)

        model._open_stream = bounded
        try:
            answer = ''.join([part async for part in model.stream_reply(uuid4(),
                'No, it is October fourth. Search the current official schedule and correct the race date.',
                [{'role': 'user', 'content': 'When is the 2026 Bahrain Grand Prix in Malaysia?'},
                 {'role': 'assistant', 'content': 'March 24. Would you like another source?'}])])
            print('Verified live answer:', answer)
            print('Model requests:', requests)
            if 'March 24' in answer or 'march 24' in answer.lower():
                raise RuntimeError('Regression: stale March 24 answer survived.')
        finally:
            await model.close()


if __name__ == '__main__':
    asyncio.run(main())
