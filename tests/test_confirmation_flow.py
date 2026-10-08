import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from athena.coordinator import VoiceCoordinator
from athena.tools.command import CommandTool
from athena.tools.registry import ToolRegistry
from athena.tools.pc_transfer import UploadTool


class ConfirmationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_memos_wait_for_pending_conversation(self):
        c = VoiceCoordinator.__new__(VoiceCoordinator)
        c.alerts = Mock()
        c.background = SimpleNamespace(jobs={"pending": object()})
        self.assertFalse(await c._offer_brief())
        c.alerts.brief_rows.assert_not_called()

    async def test_old_memos_are_not_offered_and_new_memos_only_once(self):
        c = VoiceCoordinator.__new__(VoiceCoordinator)
        c.background = SimpleNamespace(jobs={})
        c.alerts = Mock()
        c.alerts.brief_rows.return_value = [{"key": "cj", "label": "old CJ", "ready_at": "2020-01-01T00:00:00+00:00"}]
        c._speak_text = AsyncMock()
        c.quiet_hours = lambda: False
        self.assertFalse(await c._offer_brief())
        c.alerts.brief_rows.return_value = [{"key": "cj", "label": "new CJ"}]
        self.assertTrue(await c._offer_brief())
        c._brief_offer = None
        self.assertFalse(await c._offer_brief())
        c._speak_text.assert_awaited_once()

    async def test_yes_is_conversation_without_pending_action(self):
        registry = ToolRegistry()
        for reply in ("yes", "sure", "proceed", "do it"):
            self.assertIsNone(await registry.handle_user_command(reply))
            self.assertFalse(registry.has_pending_approval)

    def test_readonly_allowlist_does_not_authorize_shell_injection(self):
        tool = CommandTool()
        self.assertTrue(tool.can_run_unattended({"shell": "bash", "command": "pwd"}))
        for command in ("pwd; rm file", "ls $(touch injected)", "ls > file", "python script.py", "rm -rf data"):
            self.assertFalse(tool.can_run_unattended({"shell": "bash", "command": command}))

    async def test_last_verified_artifact_can_be_selected_without_guessed_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/"coding"/"hello").mkdir(parents=True)
            file = root/"coding"/"hello"/"main.py"; file.write_text("print('Hello world')")
            with patch.dict(os.environ, {"ATHENA_DATA_DIR": str(root),
                    "ATHENA_PC_UPLOAD_URL": "http://192.168.33.187:8781/upload",
                    "ATHENA_PC_TRANSFER_KEY": "test"*16}):
                tool = UploadTool(); tool.coding = SimpleNamespace(last_artifact=str(file))
                with patch('athena.tools.pc_transfer.resolve_pc_url', AsyncMock(return_value='http://192.168.33.187:8781/upload')):
                    prepared, spoken = await tool.prepare({})
                self.assertEqual(prepared["path"], str(file))
                self.assertIn("Say yes", spoken)

    async def test_actual_answer_precedes_memo_offer(self):
        c = VoiceCoordinator.__new__(VoiceCoordinator)
        c._listen_task = None
        c.memory = SimpleNamespace(remember_turn=AsyncMock())
        events = []
        c._speak_text = AsyncMock(side_effect=lambda text, **kwargs: events.append("answer"))
        c._offer_brief = AsyncMock(side_effect=lambda: events.append("memo"))
        c.background = SimpleNamespace(delivered=Mock(), is_confirmation_reply=lambda text: False)
        job = SimpleNamespace(id=uuid4(), text="hello", reply_started=False, reply="Hello.")
        await c._deliver(job, False)
        self.assertEqual(events, ["answer", "memo"])

    async def test_memo_does_not_steal_real_approval(self):
        c = VoiceCoordinator.__new__(VoiceCoordinator)
        c._listen_task = None
        c.memory = SimpleNamespace(remember_turn=AsyncMock())
        c._speak_text = AsyncMock()
        c._offer_brief = AsyncMock()
        c.background = SimpleNamespace(delivered=Mock(), is_confirmation_reply=lambda text: True)
        await c._deliver(SimpleNamespace(id=uuid4(), text="upload", reply_started=False, reply="Send file? Say yes."), False)
        c._offer_brief.assert_not_awaited()
