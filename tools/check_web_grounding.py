"""Silent real-model checks against controlled sources; no personal chat writes."""
import asyncio
import argparse
import os
import re
from pathlib import Path
import tempfile
from uuid import uuid4
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry
from athena.tools.models import ToolResult
from athena.tools.web import ReadWebpageTool, SearchWebTool
from athena.tools._http import WebResponse

BASE = 'https://example.com'
CALENDAR = '''<main>
<a href="/cedar"><div><h2>Cedar festival</h2><p>Attendance report: 999 visitors. Completed event.</p></div></a>
<a href="/marble"><div><h2>Marble festival</h2><p>Event details and attendance report available on the detail page.</p></div></a>
</main>'''
DETAIL = '<main><h2>Marble festival attendance report</h2><table><tr><th>Event</th><th>Visitors</th></tr><tr><td>Marble festival</td><td>742</td></tr></table></main>'


class FixtureHTTP:
    async def get(self, url):
        body = CALENDAR if url.endswith('/calendar') else DETAIL
        return WebResponse(url, body.encode(), 'text/html')


class FixtureSearch:
    definition = SearchWebTool.definition

    def __init__(self, empty_first):
        self.queries = []
        self.empty_first = empty_first

    async def execute(self, arguments):
        self.queries.append(arguments['query'])
        if self.empty_first and len(self.queries) == 1:
            return ToolResult(True, 'Three results returned; none support the requested event.', {
                'results': [{'title': 'Marble countertop cleaning'}, {'title': 'Festival dictionary definition'},
                            {'title': 'Unrelated travel advertisement'}], 'untrusted_content': True})
        return ToolResult(True, 'Relevant source link found.', {'results': [{
            'title': 'Marble festival official attendance report',
            'url': BASE + ('/marble' if self.empty_first else '/calendar'),
            'snippet': 'Open the linked page for the attendance figure.'}], 'untrusted_content': True})


async def case(empty_first, directory):
    registry = ToolRegistry()
    search = FixtureSearch(empty_first)
    registry.register(search)
    registry.register(ReadWebpageTool(FixtureHTTP()))
    calls = []
    execute = registry.execute

    async def tracked(name, arguments, **kwargs):
        result = await execute(name, arguments, **kwargs)
        calls.append({'tool': name, 'arguments': arguments, 'success': result.success})
        return result

    registry.execute = tracked
    model = DeepSeekLanguageModel(os.environ['DEEPSEEK_API_KEY'],
        os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash'), registry,
        RuntimeSettingsStore(Path(directory)/'settings.json'))
    requests = 0
    opened = model._open_stream

    async def bounded(request):
        nonlocal requests
        requests += 1
        if requests > 6:
            raise RuntimeError('Six-request model budget exhausted.')
        return await opened(request)

    model._open_stream = bounded
    try:
        answer = ''.join([part async for part in model.stream_reply(uuid4(),
            'Search online for the visitor attendance of the Marble festival. Give the exact number from its report.')])
        print('CASE:', 'unusable first search' if empty_first else 'calendar missing requested fact', flush=True)
        print('ANSWER:', ascii(answer), flush=True)
        print('CALLS:', calls, '| MODEL REQUESTS:', requests, flush=True)
        if empty_first:
            assert len(search.queries) >= 2, 'Model did not retry the unusable search.'
            assert len(set(q.casefold().strip() for q in search.queries)) >= 2, 'Retry did not change query.'
        else:
            # Rejecting Cedar's 999 in an explanation is correct, not a failure.
            assert re.search(r'(?:lists?|reported|recorded|was|is|had|of|shows|count\w*|report)\b.{0,80}\b742\b|\b742\s+visitors', answer, re.I), 'Answer did not identify the requested figure.'
            assert not re.search(r'Marble festival(?:\W+\w+){0,4}\W+999\b', answer, re.I), 'Wrong event figure was attributed to Marble.'
            reads = [row['arguments'] for row in calls if row['tool'] == 'read_webpage']
            assert any(row['url'].endswith('/calendar') for row in reads), 'Calendar was not inspected.'
            assert any(row['url'].endswith('/marble') for row in reads), 'Model did not follow the detail link.'
            assert any('marble' in row.get('query', '').casefold() for row in reads), 'Model did not focus the reader.'
        print('PASS', flush=True)
    finally:
        await model.close()
        await registry.close()


async def main(selected='both'):
    load_local_environment()
    # Public reader: a real network request on the deployed Pi, no model/audio.
    reader = ReadWebpageTool()
    args = {'url': 'https://www.formula1.com/en/racing/2026', 'query': 'Bahrain', 'max_chars': 6000}
    result = await reader.execute(args)
    assert result.success, result.message
    assert 'Azerbaijan' not in result.data['text'], 'Neighbouring race leaked into focused text.'
    assert any('/bahrain' in row['url'] for row in result.data['links']), 'Detail link missing.'
    cached = await reader.execute(args)
    assert cached.data.get('cache_hit'), 'Cache was not used.'
    print('LIVE READER: PASS | characters:', len(result.data['text']), '| cache hit: True', flush=True)
    with tempfile.TemporaryDirectory() as directory:
        if selected in {'both', 'retry'}:
            await case(True, directory)
        if selected in {'both', 'detail'}:
            await case(False, directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=['both', 'retry', 'detail'], default='both')
    asyncio.run(main(parser.parse_args().case))
