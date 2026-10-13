import asyncio
import tempfile
import unittest
from pathlib import Path
from athena.gateway import TextCursor, Gateway
from athena.task_store import TaskStore
from athena.core_client import timed_fragments


class CursorTests(unittest.TestCase):
    def test_cumulative_final_is_not_repeated(self):
        cursor = TextCursor()
        self.assertEqual(cursor.feed('Hello'), 'Hello')
        self.assertEqual(cursor.feed('Hello world'), ' world')
        self.assertEqual(cursor.feed('Hello world'), '')
        self.assertEqual(cursor.feed('Changed prefix'), '')


class StoreTests(unittest.TestCase):
    def test_repeated_submission_id_and_event_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / 'tasks.sqlite')
            try:
                first, fresh = store.create('One objective', directory, 'codex', 'request-1')
                second, duplicate = store.create('Different objective', directory, 'openclaw', 'request-1')
                self.assertTrue(fresh)
                self.assertFalse(duplicate)
                self.assertEqual(first['id'], second['id'])
                self.assertEqual(second['objective'], 'One objective')
                event = store.event(first['id'], 'activity', {'tool': 'write'})
                self.assertEqual(store.events(first['id'], event['seq']), [])
                store.update(first['id'], state='unconfirmed')
                self.assertEqual(store.get(first['id'])['state'], 'unconfirmed')
            finally: store.close()


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_can_share_last_delta_sequence(self):
        gateway = Gateway()
        async def rpc(*args, **kwargs):
            queue = next(iter(gateway.listeners))
            for state in ('delta', 'final'):
                await queue.put({'event': 'chat', 'payload': {'runId': 'run-1', 'seq': 9,
                    'state': state, 'message': {'content': [{'type': 'text', 'text': 'READY'}]}}})
            return {'runId': 'run-1'}
        gateway.rpc = rpc
        events = [event async for event in gateway.stream('session', 'hello', timeout=1)]
        self.assertEqual([event['type'] for event in events], ['accepted', 'text', 'final'])
        self.assertEqual(events[-1]['text'], 'READY')

    async def test_flush_tick_does_not_cancel_pending_token(self):
        resumed = False
        async def fragments():
            nonlocal resumed
            yield 'A long enough unfinished phrase'
            await asyncio.sleep(.08)
            resumed = True
            yield ' and its ending.'
        values = [value async for value in timed_fragments(fragments(), delay=.02)]
        self.assertTrue(resumed)
        self.assertIn(None, values)
        self.assertEqual(''.join(value for value in values if value is not None),
                         'A long enough unfinished phrase and its ending.')

    async def test_cancel_reaps_pending_token(self):
        finished = asyncio.Event()
        async def fragments():
            try:
                yield 'Enough text to start a flush timer'
                await asyncio.sleep(20)
            finally: finished.set()
        iterator = timed_fragments(fragments(), delay=.01)
        await anext(iterator)
        await iterator.aclose()
        self.assertTrue(finished.is_set())
