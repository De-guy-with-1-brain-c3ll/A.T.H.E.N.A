import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import AsyncMock

from athena.feishu import FeishuAccess, FeishuGateway, FeishuTransport, IncomingMessage
from athena.tools.registry import ToolRegistry


class FakeModel:
    def __init__(self, release=None):
        self._tools = ToolRegistry()
        self.release = release or asyncio.Event()

    def fork(self):
        return FakeModel(self.release)

    def is_confirmation_reply(self, text):
        return self._tools.is_confirmation_reply(text)

    async def stream_reply(self, turn, text, context=None, *, on_connected=None):
        if on_connected:
            on_connected()
        if text == "slow":
            await self.release.wait()
        yield "Answer: " + text


class FeishuTests(unittest.IsolatedAsyncioTestCase):
    def message(self, identity="event-1", text="hello", sender="ou_owner"):
        return IncomingMessage(identity, "message-1", sender, "p2p", "text", text)

    async def test_pairing_is_one_time_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allowed.json"
            access = FeishuAccess(path)
            self.assertEqual(access.authorize("ou_owner", "wrong"), "denied")
            self.assertEqual(access.authorize("ou_owner", f"/pair {access.pairing_code}"), "paired")
            self.assertEqual(access.authorize("ou_owner", "hello"), "allowed")
            self.assertEqual(json.loads(path.read_text())["allowed_open_ids"], ["ou_owner"])

    async def test_message_returns_immediately_while_work_continues_then_delivers(self):
        sent = []
        async def send(recipient, text):
            sent.append((recipient, text))
        model = FakeModel()
        memory = NS(context_messages=lambda: [], remember_turn=AsyncMock())
        access = FeishuAccess(Path("unused.json"), "ou_owner")
        gateway = FeishuGateway(model, memory, model._tools, send, access)
        await gateway.handle(self.message(text="slow"))
        job = next(iter(gateway.background.jobs.values()))
        self.assertFalse(job.task.done())
        self.assertIn("Task accepted", sent[0][1])
        model.release.set()
        await job.task
        await gateway.deliver_ready()
        self.assertEqual(sent[-1], ("ou_owner", "Answer: slow"))
        memory.remember_turn.assert_awaited_once()
        self.assertFalse(gateway.background.jobs)

    async def test_duplicate_group_and_unknown_senders_cannot_start_tasks(self):
        send = AsyncMock()
        model = FakeModel()
        memory = NS(context_messages=lambda: [], remember_turn=AsyncMock())
        gateway = FeishuGateway(model, memory, model._tools, send,
            FeishuAccess(Path("unused.json"), "ou_owner"))
        group = IncomingMessage("group", "m", "ou_owner", "group", "text", "hello")
        await gateway.handle(group)
        await gateway.handle(group)
        await gateway.handle(self.message(identity="unknown", sender="ou_other"))
        self.assertFalse(gateway.background.jobs)
        send.assert_awaited_once()
        self.assertIn("not paired", send.await_args.args[1])

    async def test_sdk_event_parser_extracts_only_user_text(self):
        data = NS(header=NS(event_id="evt"), event=NS(
            sender=NS(sender_type="user", sender_id=NS(open_id="ou_owner")),
            message=NS(message_id="msg", chat_type="p2p", message_type="text",
                       content='{"text":"hello"}')))
        parsed = FeishuTransport.parse_event(data)
        self.assertEqual(parsed.message_id, "msg")
        self.assertEqual((parsed.event_id, parsed.open_id, parsed.text),
                         ("evt", "ou_owner", "hello"))
        data.event.sender.sender_type = "app"
        self.assertIsNone(FeishuTransport.parse_event(data))

    async def test_help_is_local_and_send_failure_does_not_kill_gateway(self):
        send = AsyncMock(side_effect=OSError("offline"))
        model = FakeModel()
        model.usage_estimate = {"requests": 0, "estimated_input_tokens": 0,
                                "estimated_output_tokens": 0}
        memory = NS(context_messages=lambda: [], remember_turn=AsyncMock())
        gateway = FeishuGateway(model, memory, model._tools, send,
            FeishuAccess(Path("unused.json"), "ou_owner"))
        await gateway.handle(self.message(text="/help"))
        self.assertFalse(gateway.background.jobs)
        send.assert_awaited_once()

    async def test_invalid_allowlist_file_does_not_authorize_string_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allowed.json"
            path.write_text('{"allowed_open_ids": "ou_attacker"}', encoding="utf-8")
            access = FeishuAccess(path)
            self.assertEqual(access.authorize("o", "hello"), "denied")

    async def test_failed_final_delivery_is_not_written_to_conversation_memory(self):
        send = AsyncMock(side_effect=[None, OSError('offline')])
        model = FakeModel()
        memory = NS(context_messages=lambda: [], remember_turn=AsyncMock())
        gateway = FeishuGateway(model, memory, model._tools, send,
            FeishuAccess(Path('unused.json'), 'ou_owner'))
        await gateway.handle(self.message(text='hello'))
        job = next(iter(gateway.background.jobs.values()))
        await job.task
        await gateway.deliver_ready()
        memory.remember_turn.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
