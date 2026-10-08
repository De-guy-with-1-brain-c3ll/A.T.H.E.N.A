import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
from athena.memory.database import MemoryDatabase, StoredTurn
from athena.memory.service import MemoryService
from athena.tools.models import ToolDefinition, ToolResult
from athena.tools.registry import ToolRegistry
from athena.web_evidence import record, recent

class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_web_output_is_recorded_without_extra_model_calls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ATHENA_DATA_DIR': directory}):
            result = ToolResult(True, 'Retrieved.', {'text': '<script>not executed</script> Winner: VER', 'url': 'https://example.com/results'})
            registry = ToolRegistry()
            tool = SimpleNamespace(definition=ToolDefinition('read_webpage', 'test', {'type':'object'}), execute=AsyncMock(return_value=result))
            registry.register(tool)
            reply = await registry.execute('read_webpage', {'url': 'https://example.com/results'})
            rows = recent()
            self.assertEqual(rows[0]['data'], result.data)
            self.assertEqual(rows[0]['operation_id'], reply.data['operation_id'])
            self.assertEqual(rows[0]['request']['url'], 'https://example.com/results')
            for i in range(35):
                record('search_web', {'query': str(i), 'api_key': 'never-store'}, result, str(i))
            self.assertEqual(len(recent()), 30)
            self.assertNotIn('api_key', recent()[0]['request'])

    async def test_clear_is_shared_preserves_history_and_blocks_old_queued_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'memory.db'
            db = MemoryDatabase(path); db.initialize()
            old = StoredTurn(uuid4(), 'old question', 'old answer')
            db.save_turn(old); db.save_summary('old summary')
            memory = MemoryService(path, 'test', 'test')
            await memory.connect()
            try:
                self.assertTrue(memory.context_messages())
                db.clear_context()
                self.assertEqual(memory.context_messages(), [])
                db.save_turn(old)  # An old queued write must not cross the boundary.
                self.assertEqual(db.recent_turns(), [])
                self.assertEqual(len(db.recent_conversations()), 1)
                db.save_turn(StoredTurn(uuid4(), 'new question', 'new answer'))
                self.assertEqual(len(db.recent_turns()), 1)
                self.assertEqual(len(db.recent_conversations(1, 1)), 1)
            finally:
                await memory.close()

    def test_inspector_renders_untrusted_output_as_text(self):
        root=Path(__file__).parents[1]/'src/athena/web_static'
        js=(root/'app.js').read_text(encoding='utf-8')
        page=(root/'index.html').read_text(encoding='utf-8')
        self.assertIn('pre.textContent=JSON.stringify(row', js)
        for identity in ('clearContext','olderHistory','evidenceList','refreshEvidence'):
            self.assertIn('id="'+identity+'"', page)
