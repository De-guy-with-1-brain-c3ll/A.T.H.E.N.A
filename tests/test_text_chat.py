import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from athena.text import TextSession, console_input
from athena.llm.deepseek import DeepSeekUnavailable


class TextChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_input_uses_cancellable_daemon_bridge(self):
        with patch("builtins.input", return_value="hello"):
            self.assertEqual(await console_input("You: "), "hello")

    async def test_text_reply_uses_memory_and_remembers_completed_turn(self):
        async def stream(turn, text, context):
            self.assertEqual(text, "hello")
            self.assertEqual(context, [{"role": "system", "content": "memory"}])
            yield "Hello"
            yield " there."
        model = MagicMock()
        model.stream_reply = stream
        memory = MagicMock()
        memory.context_messages.return_value = [{"role": "system", "content": "memory"}]
        memory.remember_turn = AsyncMock()
        session = TextSession(model, memory, MagicMock())
        answer = await session.reply("hello")
        self.assertEqual(answer, "Hello there.")
        memory.remember_turn.assert_awaited_once()
        args = memory.remember_turn.await_args.args
        self.assertEqual(args[1:], ("hello", "Hello there."))

    async def test_empty_response_is_not_saved(self):
        async def stream(*args):
            if False:
                yield ""
        memory = MagicMock()
        memory.context_messages.return_value = []
        memory.remember_turn = AsyncMock()
        session = TextSession(type("Model", (), {"stream_reply": stream})(), memory, MagicMock())
        self.assertEqual(await session.reply("hello"), "")
        memory.remember_turn.assert_not_awaited()

    async def test_provider_connection_error_does_not_escape_or_enter_memory(self):
        async def stream(*args):
            raise DeepSeekUnavailable("I couldn't connect to DeepSeek. Try again.")
            if False:
                yield ""
        memory = MagicMock()
        memory.context_messages.return_value = []
        memory.remember_turn = AsyncMock()
        session = TextSession(type("Model", (), {"stream_reply": stream})(), memory, MagicMock())
        self.assertEqual(await session.reply("hello"), "")
        memory.remember_turn.assert_not_awaited()
