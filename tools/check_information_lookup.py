"""Silent public-information smoke tests; at most four model calls per case."""
import asyncio
import os
from pathlib import Path
import tempfile
from uuid import uuid4
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry
from athena.tools.web import SearchWebTool, ReadWebpageTool


async def main():
    load_local_environment()
    questions = [
        "What's the latest stable sherpa-onnx release version?",
        "What changed in the Formula 1 calendar recently about Bahrain and Malaysia?",
        "Who received the 2026 Ig Nobel physics prize, and for what research?",
    ]
    for question in questions:
        registry = ToolRegistry()
        registry.register(SearchWebTool()); registry.register(ReadWebpageTool())
        searches = []
        execute = registry.execute

        async def tracked(name, args):
            if name == 'search_web': searches.append(args.get('query', ''))
            return await execute(name, args)

        registry.execute = tracked
        with tempfile.TemporaryDirectory() as directory:
            model = DeepSeekLanguageModel(os.environ['DEEPSEEK_API_KEY'],
                os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash'), registry,
                RuntimeSettingsStore(Path(directory)/'settings.json'))
            opened = model._open_stream
            requests = 0

            async def bounded(request):
                nonlocal requests
                requests += 1
                if requests > 4: raise RuntimeError('Four-request limit reached.')
                return await opened(request)

            model._open_stream = bounded
            try:
                answer = ''.join([part async for part in model.stream_reply(uuid4(), question)])
                print('Question:', question, flush=True)
                print('Answer:', answer, flush=True)
                print('Searches:', len(searches), '| Model requests:', requests, flush=True)
                if not searches: raise RuntimeError('No lookup performed for unfamiliar/current information.')
                if 'would you like me to search' in answer.lower():
                    raise RuntimeError('Search-permission question survived.')
            finally:
                await model.close()


if __name__ == '__main__': asyncio.run(main())
