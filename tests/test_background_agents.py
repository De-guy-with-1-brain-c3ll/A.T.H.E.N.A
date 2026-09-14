import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from athena.background import BackgroundAgents
from athena.coordinator import VoiceCoordinator
from athena.audio.vad import VoiceGate
from athena.events import AudioChunk
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.settings.store import RuntimeSettingsStore
from athena.tools.registry import ToolRegistry
from athena.tts.qwen import QwenRealtimeSynthesizer


class FakeModel:
    def __init__(self, release=None):
        self._tools = ToolRegistry()
        self.release = release or asyncio.Event()
        self.started = asyncio.Queue()
        self.cancelled = asyncio.Event()
        self.shutdown_requested = False

    def fork(self):
        other = FakeModel(self.release)
        other.started = self.started
        other.cancelled = self.cancelled
        return other

    def is_confirmation_reply(self, text):
        return self._tools.is_confirmation_reply(text)

    async def stream_reply(self, turn, text, context=None, *, on_connected=None):
        await self.started.put(text)
        if on_connected:
            on_connected()
        if text == 'slow search':
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if text.startswith('download'):
            self._tools._pending = ('download_file', {'url': text}, time.monotonic() + 120)
            yield 'Approve ' + text
        elif text == 'yes':
            pending, self._tools._pending = self._tools._pending, None
            yield 'Approved ' + pending[1]['url'] if pending else 'No pending request'
        else:
            yield 'Answer: ' + text


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequential_chat_reuses_root_and_releases_completed_job(self):
        model = FakeModel()
        hub = BackgroundAgents(model)
        job = hub.submit('hello', [])
        self.assertIs(job.model, model)
        self.assertFalse(job.forked)
        await job.task
        hub.delivered(job)
        self.assertIsNone(job.model)
        self.assertIsNone(job.task)

    async def test_slow_search_does_not_block_second_answer_or_ack(self):
        model = FakeModel()
        hub = BackgroundAgents(model)
        slow = hub.submit('slow search', [])
        await model.started.get()
        self.assertEqual(hub.next_output(), (slow, True))
        slow.acknowledged = True
        fast = hub.submit('hello', [])
        await fast.task
        self.assertEqual(hub.next_output(), (fast, False))
        self.assertFalse(slow.task.done())
        self.assertEqual(fast.reply, 'Answer: hello')
        await hub.cancel_all()
        self.assertTrue(model.cancelled.is_set())

    async def test_bound_and_cancellation_remove_stale_results(self):
        hub = BackgroundAgents(FakeModel(), limit=2)
        hub.submit('slow search', [])
        hub.submit('slow search', [])
        self.assertIsNone(hub.submit('third', []))
        await asyncio.sleep(0)
        await hub.cancel_all()
        self.assertIsNone(hub.next_output())
        self.assertFalse(hub.jobs)

    async def test_slow_connection_gets_one_ack_without_claiming_success(self):
        class SlowConnection(FakeModel):
            def fork(self):
                return self
            async def stream_reply(self, *args, on_connected=None):
                await self.release.wait()
                on_connected()
                yield 'Done'
        hub = BackgroundAgents(SlowConnection())
        job = hub.submit('hello', [])
        await asyncio.wait_for(hub.changed.wait(), 1.5)
        self.assertIsNone(job.reply)
        self.assertEqual(hub.next_output(), (job, True))
        job.acknowledged = True
        hub.model.release.set()
        await job.task
        self.assertEqual(hub.next_output(), (job, False))
        hub.delivered(job)
        self.assertIsNone(hub.next_output())

    async def test_fast_result_skips_ack_and_errors_do_not_kill_other_jobs(self):
        class FailingModel(FakeModel):
            def fork(self):
                return self
            async def stream_reply(self, *args, **kwargs):
                raise RuntimeError('private provider diagnostic')
                if False:
                    yield ''
        hub = BackgroundAgents(FakeModel())
        fast = hub.submit('hello', [])
        await fast.task
        self.assertEqual(hub.next_output(), (fast, False))
        hub.delivered(fast)
        hub.model = FailingModel()
        failed = hub.submit('bad', [])
        await failed.task
        self.assertEqual(failed.reply, 'That request failed. Please try again.')
        self.assertNotIn('private', failed.reply)
        await hub.cancel_all()

    async def test_safe_provider_error_remains_actionable(self):
        from athena.llm.deepseek import DeepSeekUnavailable
        class Offline(FakeModel):
            async def stream_reply(self, *args, **kwargs):
                raise DeepSeekUnavailable('DeepSeek is temporarily unavailable.')
                if False:
                    yield ''
        hub = BackgroundAgents(Offline())
        job = hub.submit('hello', [])
        await job.task
        self.assertEqual(job.reply, 'DeepSeek is temporarily unavailable.')
        await hub.cancel_all()

    async def test_approvals_are_isolated_and_only_presented_one_at_a_time(self):
        hub = BackgroundAgents(FakeModel())
        first = hub.submit('download A', [])
        second = hub.submit('download B', [])
        await asyncio.gather(first.task, second.task)
        self.assertFalse(hub.is_confirmation_reply('yes'))
        hub.delivered(first)
        self.assertTrue(hub.is_confirmation_reply('yes'))
        self.assertIsNone(hub.next_output())  # B cannot overwrite the spoken grant.
        approval = hub.submit('yes', [])
        await approval.task
        self.assertEqual(approval.reply, 'Approved download A')
        self.assertIsNotNone(second.model._tools._pending)
        await hub.cancel_all()

    async def test_queued_approval_expiry_starts_at_delivery(self):
        hub = BackgroundAgents(FakeModel())
        job = hub.submit('download A', [])
        await job.task
        name, args, _ = job.model._tools._pending
        job.model._tools._pending = (name, args, 0)
        hub.delivered(job)
        self.assertTrue(hub.is_confirmation_reply('yes'))
        job.model._tools._pending = (name, args, 0)
        self.assertIsNone(hub.next_output())
        self.assertIsNone(hub.approval_model)

    async def test_unanswered_approval_worker_is_released_after_expiry(self):
        hub = BackgroundAgents(FakeModel())
        hub.APPROVAL_LIFETIME_SECONDS = 0.01
        job = hub.submit('download A', [])
        await job.task
        worker = job.model
        hub.delivered(job)
        self.assertIs(hub.approval_model, worker)
        await asyncio.sleep(0.03)
        self.assertIsNone(hub.approval_model)
        self.assertIsNone(worker._tools._pending)

    async def test_stream_open_event_precedes_first_token_and_cancel_closes_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            model = DeepSeekLanguageModel('test', 'test', ToolRegistry(),
                RuntimeSettingsStore(Path(directory) / 'settings.json'))
            entered, opened = asyncio.Event(), asyncio.Event()
            class SlowStream:
                close = AsyncMock()
                async def __aiter__(self):
                    entered.set()
                    await asyncio.Event().wait()
                    if False:
                        yield None
            stream = SlowStream()
            model._client.chat.completions.create = AsyncMock(return_value=stream)
            async def consume():
                return [part async for part in model.stream_reply(uuid4(), 'hello', on_connected=opened.set)]
            task = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertTrue(opened.is_set())
                self.assertFalse(task.done())
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                stream.close.assert_awaited_once()
            finally:
                await model.close()

    async def test_registry_fork_does_not_copy_authorization(self):
        registry = ToolRegistry()
        registry._pending = ('download_file', {}, time.monotonic() + 120)
        registry._shutdown_authorized = True
        fork = registry.fork()
        self.assertIsNone(fork._pending)
        self.assertFalse(fork._shutdown_authorized)

    async def test_tts_ignores_old_session_end_markers(self):
        tts = QwenRealtimeSynthesizer('test', 'test', 'test')
        await tts.connect()
        old, current = uuid4(), uuid4()
        for item in (old, AudioChunk(old, b'old'), AudioChunk(current, b'new'), old, current):
            tts._publish(item)
        chunks = await asyncio.wait_for(self.collect(tts.audio(current)), 1)
        self.assertEqual([c.pcm for c in chunks], [b'new'])

    @staticmethod
    async def collect(iterator):
        return [item async for item in iterator]

    async def test_floor_is_reserved_before_full_voice_activation(self):
        gate = VoiceGate()
        coordinator = VoiceCoordinator(*([MagicMock()] * 6), gate, MagicMock())
        self.assertTrue(coordinator._safe_to_speak())
        gate._consecutive_voiced = 1
        self.assertFalse(coordinator._safe_to_speak())
        gate._consecutive_voiced = 0
        gate.active = True
        self.assertFalse(coordinator._safe_to_speak())

    async def test_coordinator_listens_to_another_request_during_slow_work(self):
        model = FakeModel()
        memory = NS(context_messages=lambda: [], remember_turn=AsyncMock())
        coordinator = VoiceCoordinator(MagicMock(), MagicMock(), MagicMock(), model,
            MagicMock(), memory, VoiceGate(), MagicMock())
        calls = 0
        async def listen(turn):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 'slow search'
            if calls == 2:
                await model.started.get()
                self.assertFalse(next(iter(coordinator.background.jobs.values())).task.done())
                return 'hello'
            await asyncio.Event().wait()
        delivered = asyncio.Event()
        async def deliver(job, acknowledgement):
            await coordinator._stop_listening()
            if acknowledgement:
                job.acknowledged = True
            else:
                self.assertEqual(job.reply, 'Answer: hello')
                coordinator.background.delivered(job)
                delivered.set()
                model.shutdown_requested = True
        coordinator._listen = listen
        coordinator._deliver = deliver
        await asyncio.wait_for(coordinator.run(), 2)
        self.assertTrue(delivered.is_set())
        self.assertTrue(model.cancelled.is_set())

    async def test_ack_playback_does_not_wait_for_stt_stop(self):
        model = FakeModel()
        speaker = NS(play=AsyncMock())
        coordinator = VoiceCoordinator(MagicMock(), speaker, MagicMock(), model,
            MagicMock(), MagicMock(), VoiceGate(), MagicMock())
        stopped = asyncio.Event()
        started = asyncio.Event()
        async def listening():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await stopped.wait()
        coordinator._listen_task = asyncio.create_task(listening())
        await started.wait()
        job = NS(id=uuid4(), acknowledged=False)
        coordinator._ack_pcm = b'cached speech'
        delivery = asyncio.create_task(coordinator._deliver(job, True))
        await asyncio.sleep(0)
        speaker.play.assert_awaited_once_with(b'cached speech')
        self.assertFalse(delivery.done())
        stopped.set()
        await delivery

    async def test_shutdown_cancels_running_job_before_goodbye(self):
        model = FakeModel()
        coordinator = VoiceCoordinator(MagicMock(), MagicMock(), MagicMock(), model,
            MagicMock(), NS(context_messages=lambda: []), VoiceGate(), MagicMock())
        count = 0
        async def listen(turn):
            nonlocal count
            count += 1
            if count == 1:
                return 'slow search'
            await model.started.get()
            return 'ATHENA shut down'
        async def goodbye(turn, text):
            self.assertTrue(model.cancelled.is_set())
            model.shutdown_requested = True
        coordinator._listen = listen
        coordinator._answer = AsyncMock(side_effect=goodbye)
        await asyncio.wait_for(coordinator.run(), 2)
        coordinator._answer.assert_awaited_once()
        self.assertFalse(coordinator.background.jobs)
