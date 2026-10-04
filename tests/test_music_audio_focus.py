import asyncio
from pathlib import Path
import tempfile
import shutil
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from athena.audio.playback import Speaker
from athena.coordinator import VoiceCoordinator
from athena.events import AudioChunk
from athena.remote_audio import BrowserSpeaker, RemoteAudio
from athena.tools.netease import NetEasePlayer, NetEaseMusicTool
from athena.tools.registry import ToolRegistry


class FakeSpeaker:
    def __init__(self):
        self.packets=[]
        self.stop=AsyncMock()
        self.finish_playback=AsyncMock()
    async def play(self, pcm, *format):
        self.packets.append(pcm)
        await asyncio.sleep(0)


class FocusTests(unittest.IsolatedAsyncioTestCase):
    def player(self, speaker):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return NetEasePlayer(speaker,Path(directory.name)/'playlists.json')

    def coordinator(self, player, speaker):
        coordinator=VoiceCoordinator.__new__(VoiceCoordinator)
        tool=NetEaseMusicTool()
        tool.player=player
        registry=ToolRegistry()
        registry.register(tool)
        coordinator.llm=NS(_tools=registry)
        coordinator.speaker=speaker
        coordinator.active_turn=uuid4()
        return coordinator

    async def test_streamed_reply_holds_music_for_all_chunks_not_just_each_write(self):
        speaker=FakeSpeaker()
        player=self.player(speaker)
        coordinator=self.coordinator(player,speaker)
        first_audio=asyncio.Event()
        allow_last=asyncio.Event()
        async def audio(turn):
            await first_audio.wait()
            yield AudioChunk(turn,b'voice1')
            await allow_last.wait()
            yield AudioChunk(turn,b'voice2')
        coordinator.tts=NS(audio=audio)
        playback=asyncio.create_task(coordinator._play_audio(coordinator.active_turn))
        try:
            await player._play_music_chunk(b'music1',24000,1,player._track_generation)
            first_audio.set()
            for _ in range(20):
                if b'voice1' in speaker.packets: break
                await asyncio.sleep(.001)
            self.assertIn(b'voice1',speaker.packets)
            music=asyncio.create_task(player._play_music_chunk(b'music2',24000,1,player._track_generation))
            await asyncio.sleep(.01)
            self.assertFalse(music.done())
            allow_last.set()
            self.assertEqual(await playback,12)
            await music
            self.assertEqual(speaker.packets,[b'music1',b'voice1',b'voice2',b'music2'])
            speaker.finish_playback.assert_awaited_once()
        finally:
            playback.cancel()
            await asyncio.gather(playback,return_exceptions=True)

    async def test_nested_voice_owners_do_not_resume_music_early(self):
        player=self.player(FakeSpeaker())
        player.suspend_for_voice()
        player.suspend_for_voice()
        player.resume_after_voice()
        self.assertFalse(player._voice_clear.is_set())
        player.resume_after_voice()
        self.assertTrue(player._voice_clear.is_set())

    async def test_manual_pause_survives_voice_completion_and_works_on_windows(self):
        speaker=FakeSpeaker()
        player=self.player(speaker)
        player.process=NS()
        player.suspend_for_voice()
        with patch('athena.tools.netease.os.name','nt'):
            await player.pause()
        speaker.stop.assert_not_awaited()  # Do not flush an in-progress spoken reply.
        player.resume_after_voice()
        music=asyncio.create_task(player._play_music_chunk(b'music',24000,1,player._track_generation))
        await asyncio.sleep(.01)
        self.assertFalse(music.done())
        await player.resume()
        await asyncio.wait_for(music,1)
        self.assertEqual(speaker.packets,[b'music'])

    async def test_pause_waits_for_current_write_then_flushes_queued_music(self):
        speaker=FakeSpeaker()
        started=asyncio.Event()
        release=asyncio.Event()
        async def play(*args):
            started.set()
            await release.wait()
        speaker.play=play
        player=self.player(speaker)
        player.process=NS()
        music=asyncio.create_task(player._play_music_chunk(b'music',24000,1,player._track_generation))
        await started.wait()
        pause=asyncio.create_task(player.pause())
        await asyncio.sleep(0)
        self.assertFalse(pause.done())
        self.assertFalse(player._playback_clear.is_set())
        release.set()
        await asyncio.gather(music,pause)
        speaker.stop.assert_awaited_once()

    async def test_skipping_discards_a_music_packet_waiting_behind_voice(self):
        speaker=FakeSpeaker()
        player=self.player(speaker)
        player.process=NS(kill=MagicMock())
        generation=player._track_generation
        player.suspend_for_voice()
        music=asyncio.create_task(player._play_music_chunk(b'old-track',24000,1,generation))
        await player.next()
        player.resume_after_voice()
        self.assertFalse(await asyncio.wait_for(music,1))
        self.assertEqual(speaker.packets,[])

    async def test_voice_cancellation_releases_music_and_flushes_reply(self):
        speaker=FakeSpeaker()
        player=self.player(speaker)
        coordinator=self.coordinator(player,speaker)
        entered=asyncio.Event()
        async def speak():
            async with coordinator._audio_focus():
                entered.set()
                await asyncio.Future()
        task=asyncio.create_task(speak())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertTrue(player._voice_clear.is_set())
        self.assertEqual(player._voice_depth,0)
        speaker.stop.assert_awaited_once()

    async def test_fast_pause_changes_player_before_speaking_without_model_request(self):
        speaker=FakeSpeaker()
        player=self.player(speaker)
        player.process=NS()
        coordinator=self.coordinator(player,speaker)
        coordinator.memory=NS(remember_turn=AsyncMock())
        async def confirm(text,**kwargs):
            self.assertTrue(player.paused)
            self.assertEqual(text,'Paused.')
        coordinator._speak_text=AsyncMock(side_effect=confirm)
        self.assertTrue(await coordinator._handle_fast_music_control('pause the music'))
        self.assertEqual(coordinator.memory.remember_turn.await_args.args[1:],('pause the music','Paused.'))
        self.assertFalse(await coordinator._handle_fast_music_control('search for new music'))

    async def test_browser_final_audio_packet_finishes_before_music_can_resume(self):
        audio=RemoteAudio()
        sink=NS(closed=False,send_json=AsyncMock(),send_bytes=AsyncMock())
        audio.attach(sink)
        speaker=BrowserSpeaker(audio)
        await speaker.play(bytes(4800))  # 100-ms final speech packet.
        finish=asyncio.create_task(speaker.finish_playback())
        await asyncio.sleep(.01)
        self.assertFalse(finish.done())
        await asyncio.wait_for(finish,1)

    async def test_browser_stop_interrupts_final_packet_wait(self):
        audio=RemoteAudio()
        sink=NS(closed=False,send_json=AsyncMock(),send_bytes=AsyncMock())
        audio.attach(sink)
        speaker=BrowserSpeaker(audio)
        await speaker.play(bytes(480000))
        finish=asyncio.create_task(speaker.finish_playback())
        await asyncio.sleep(.001)
        await speaker.stop()
        await asyncio.wait_for(finish,.2)

    async def test_physical_output_uses_bounded_writes_and_real_abort_binding(self):
        speaker=Speaker()
        stream=NS(write=MagicMock(),_stream=object(),_is_running=True,start_stream=MagicMock())
        speaker._stream=stream
        await speaker.play(bytes(48000))
        self.assertEqual(stream.write.call_count,10)
        self.assertTrue(all(len(call.args[0])<=4800 for call in stream.write.call_args_list))
        with patch('athena.audio.playback.pyaudio.pa.abort_stream') as abort:
            await speaker.stop()
            abort.assert_called_once_with(stream._stream)
        self.assertFalse(stream._is_running)
        stream.start_stream.assert_called_once()

    @unittest.skipUnless(shutil.which('ffmpeg'),'Real decoder integration requires ffmpeg')
    async def test_real_decoder_pause_speech_resume_and_stop_with_full_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            wav=Path(directory)/'tone.wav'
            generator=await asyncio.create_subprocess_exec('ffmpeg','-loglevel','error',
                '-f','lavfi','-i','sine=frequency=440:duration=30','-ar','24000','-ac','1',str(wav),
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(generator.communicate(),10)
            self.assertEqual(generator.returncode,0)
            speaker=FakeSpeaker()
            speaker.music_format=(24000,1)
            original_play=speaker.play
            async def paced_play(pcm,*format):
                await original_play(pcm,*format)
                await asyncio.sleep(.02)
            speaker.play=paced_play
            player=NetEasePlayer(speaker,Path(directory)/'playlists.json')
            player._search=AsyncMock(return_value=[('1','Test tone')])
            player._stream_url=AsyncMock(return_value=str(wav))
            try:
                await player.play('test')
                await asyncio.sleep(.05)
                await player.pause()
                packets=len(speaker.packets)
                await asyncio.sleep(.06)
                self.assertEqual(len(speaker.packets),packets)
                coordinator=self.coordinator(player,speaker)
                async def audio(turn):
                    yield AudioChunk(turn,b'voice1')
                    yield AudioChunk(turn,b'voice2')
                coordinator.tts=NS(audio=audio)
                await coordinator._play_audio(coordinator.active_turn)
                self.assertTrue(player.paused)
                self.assertEqual(speaker.packets[packets:],[b'voice1',b'voice2'])
                await player.resume()
                await asyncio.sleep(.04)
                self.assertGreater(len(speaker.packets),packets+2)
            finally:
                await asyncio.wait_for(player.stop(),3)
            self.assertIsNone(player.task)
            self.assertIsNone(player.process)
