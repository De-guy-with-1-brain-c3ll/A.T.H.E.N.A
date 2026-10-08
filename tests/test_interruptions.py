import asyncio
from array import array
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
from athena.coordinator import VoiceCoordinator
from athena.events import Transcript
from athena.interruptions import hear_interruption, stop_command


class InterruptionTests(unittest.IsolatedAsyncioTestCase):
    def test_stop_commands_and_non_commands(self):
        for text in ('stop', 'Athena stop talking', 'Please stop speaking', 'Be quiet',
                     'shut up', "that's enough", 'hold on', 'Athena wait', 'stop now',
                     'okay stop', 'never mind', 'Athena stop', 'stop that'):
            self.assertTrue(stop_command(text), text)
        for text in ('The next stop is London', 'I cannot stop talking about it',
                     'Stopwatch', 'Stop the timer', 'You can say stop to interrupt me'):
            self.assertFalse(stop_command(text), text)

    def test_the_inflection_the_live_recogniser_returns_is_accepted(self):
        """The board transcribes "stop talking" as "stopped talking.".

        Taken verbatim from a live Pi run with the real cloud recogniser, where
        the final transcript was "Athena stopped talking." and the interruption
        was rejected. The trailing period is stripped by the normaliser; the
        tense is not, so it has to be handled deliberately.
        """
        for text in ('Athena stopped talking.', 'stopped talking', 'Athena stopped speaking',
                     'Stop.', 'ATHENA STOPPED TALKING', 'stopped', 'stop reading',
                     'Athena, stopped talking!'):
            self.assertTrue(stop_command(text), text)

    def test_normalising_the_tense_does_not_widen_what_counts(self):
        """The tense fix must not turn ordinary sentences into stop commands."""
        for text in ('I cannot stop talking about it', 'the stopped train was late',
                     'she is talking about the stop', 'we talked about stopping',
                     'stop talking to me'):
            self.assertFalse(stop_command(text), text)

    async def test_a_bare_stop_word_stops_once_the_speech_has_ended(self):
        """A one-word "stop" arrives as a partial and the segment then closes.

        Requiring a final transcript meant the common case never interrupted:
        the cloud recogniser ends a short clip without one.
        """
        closed = asyncio.Event()

        class Mic:
            async def frames(self):
                # Loud enough to open the gate, then silent so it closes again.
                for _ in range(40):
                    yield array('h', [1200] * 320).tobytes()
                    await asyncio.sleep(.001)
                while True:
                    yield bytes(640)
                    await asyncio.sleep(.01)
        class STT:
            def __init__(self): self.sent = asyncio.Event()
            async def start_turn(self, turn): self.turn = turn
            async def send_audio(self, pcm): self.sent.set()
            async def finish_turn(self): closed.set()
            async def results(self):
                await self.sent.wait()
                await closed.wait()
                yield Transcript(self.turn, 'Stop', False, 1.)
        stt = STT()
        result = await asyncio.wait_for(hear_interruption(Mic(), stt), 3)
        self.assertEqual(result, 'Stop')

    async def test_a_bare_stop_word_mid_speech_waits_for_more_words(self):
        """"Stop the timer" begins with the same word, so the partial must not stop.

        While the gate is still open the word is ambiguous, so the listener
        keeps waiting rather than cutting Athena off mid-sentence.
        """
        class Mic:
            async def frames(self):
                while True:
                    yield array('h', [1200] * 320).tobytes()
                    await asyncio.sleep(.001)
        class STT:
            def __init__(self): self.sent = asyncio.Event()
            async def start_turn(self, turn): self.turn = turn
            async def send_audio(self, pcm): self.sent.set()
            async def finish_turn(self): pass
            async def results(self):
                await self.sent.wait()
                yield Transcript(self.turn, 'Stop', False, 1.)
                await asyncio.Event().wait()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(hear_interruption(Mic(), STT()), .5)

    async def test_explicit_partial_stops_without_waiting_for_sentence_end(self):
        class Mic:
            async def frames(self):
                while True:
                    yield array('h', [1200] * 320).tobytes()
                    await asyncio.sleep(.001)
        class STT:
            def __init__(self): self.sent = asyncio.Event(); self.finished = False
            async def start_turn(self, turn): self.turn = turn
            async def send_audio(self, pcm): self.sent.set()
            async def finish_turn(self): self.finished = True
            async def results(self):
                await self.sent.wait()
                yield Transcript(uuid4(), 'Athena stop talking', False, 1.)
                yield Transcript(self.turn, 'The Bahrain results are here', False, 1.)
                yield Transcript(self.turn, 'Stop', False, 1.)
                yield Transcript(self.turn, 'Athena stop talking', False, 1.)
        stt = STT()
        result = await asyncio.wait_for(hear_interruption(Mic(), stt), 1)
        self.assertEqual(result, 'Athena stop talking')
        self.assertTrue(stt.finished)

    async def test_stop_cancels_speech_flushes_output_and_returns_to_run_loop(self):
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        coordinator.interruption_stt = Mock()
        coordinator.microphone = Mock()
        coordinator.settings_store = NS(get=lambda key: 400)
        coordinator._stop_listening = AsyncMock()
        coordinator.cancel_active_turn = AsyncMock()
        cancelled = asyncio.Event()
        async def speaking():
            try: await asyncio.sleep(10)
            finally: cancelled.set()
        with patch('athena.interruptions.hear_interruption', AsyncMock(return_value='stop talking')):
            await asyncio.wait_for(coordinator._interruptible_speech(speaking()), 1)
        self.assertTrue(cancelled.is_set())
        coordinator.cancel_active_turn.assert_awaited_once()

    async def test_interruption_listener_failure_does_not_drop_reply(self):
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        coordinator.interruption_stt = Mock(); coordinator.microphone = Mock()
        coordinator.settings_store = NS(get=lambda key: 400)
        coordinator._stop_listening = AsyncMock()
        complete = asyncio.Event()
        async def speaking():
            await asyncio.sleep(.02); complete.set()
        with patch('athena.interruptions.hear_interruption', AsyncMock(side_effect=RuntimeError('offline'))):
            await coordinator._interruptible_speech(speaking())
        self.assertTrue(complete.is_set())
