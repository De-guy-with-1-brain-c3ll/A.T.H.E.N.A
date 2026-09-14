from __future__ import annotations

import asyncio
import os
import random
import re
import time
from uuid import UUID, uuid4

from athena.audio.capture import Microphone
from athena.audio.playback import Speaker
from athena.audio.vad import VoiceGate
from athena.audio.telemetry import AudioStatusWriter
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.llm.speech_chunker import SpeechChunker
from athena.memory.service import MemoryService
from athena.state import AgentState
from athena.settings.store import RuntimeSettingsStore
from athena.stt.fun_asr import FunAsrRecognizer
from athena.tts.qwen import QwenRealtimeSynthesizer
from athena.background import BackgroundAgents
from athena.tools.registry import ToolRegistry


class VoiceCoordinator:
    WAKE_ACKS = ("Yes?", "What's up?", "I'm listening.", "Go ahead.",
                 "Ready.", "I'm here.", "How can I help?", "At your service.",
                 "I'm listening, Benjamin.", "What do you need?")
    def __init__(
        self,
        microphone: Microphone,
        speaker: Speaker,
        stt: FunAsrRecognizer,
        llm: DeepSeekLanguageModel,
        tts: QwenRealtimeSynthesizer,
        memory: MemoryService,
        voice_gate: VoiceGate,
        settings_store: RuntimeSettingsStore,
        audio_debug: bool = False,
    ) -> None:
        self.microphone = microphone
        self.speaker = speaker
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.memory = memory
        self.voice_gate = voice_gate
        self.settings_store = settings_store
        self.audio_debug = audio_debug
        self.audio_status = AudioStatusWriter.from_environment()
        self.state = AgentState.IDLE
        self.active_turn: UUID | None = None
        self.background = BackgroundAgents(llm)
        self._ack_pcm = b""
        self._listen_task: asyncio.Task | None = None
        self._external_speech: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
        self._external_changed = asyncio.Event()
        # Desktop/testing stays backwards-compatible; the Orange Pi env enables
        # this for its always-on microphone service.
        self.wake_word = os.environ.get("ATHENA_WAKE_WORD", "").strip().casefold()
        self._active_until = 0.0

    def enqueue_external_speech(self, text: str) -> bool:
        try:
            self._external_speech.put_nowait(text)
            self._external_changed.set()
            return True
        except asyncio.QueueFull:
            return False

    async def control_music(self, action: str, value: int | None = None) -> dict:
        tool = self.llm._tools.get("netease_music")
        player = getattr(tool, "player", None)
        if player is None:
            raise RuntimeError("Music is unavailable while ATHENA voice is offline.")
        if action == "status":
            return player.status()
        if action == "toggle":
            state = player.status()
            if not state["playing"]:
                raise RuntimeError("No track is loaded. Ask ATHENA to play something first.")
            action = "resume" if state["paused"] else "pause"
        if action == "volume":
            if value is None or not 0 <= value <= 100:
                raise RuntimeError("Volume must be between 0 and 100.")
            player.set_volume(value)
        elif action == "pause":
            await player.pause()
        elif action == "resume":
            await player.resume()
        elif action == "next":
            await player.next()
        elif action == "stop":
            await player.stop()
        else:
            raise RuntimeError("Unknown music command.")
        return player.status()

    async def connect(self) -> None:
        await asyncio.gather(
            self.microphone.open(),
            self.speaker.open(),
            self.stt.connect(),
            self.llm.connect(),
            self.tts.connect(),
            self.memory.connect(),
        )
        print("Preparing the quick acknowledgement voice...")
        try:
            self._ack_pcm = await asyncio.wait_for(self.tts.cache_phrase("On it."), 8)
        except Exception:
            print("Voice acknowledgement unavailable; using a short local tone.")
        if not self._ack_pcm:
            # Offline fallback: no additional network wait and no claim of success.
            from array import array
            import math
            import sys
            pcm = array("h", (int(2200 * math.sin(2 * math.pi * 660 * i / 24000)
                                  * math.sin(math.pi * i / 2400)) for i in range(2400)))
            if sys.byteorder != "little":
                pcm.byteswap()
            self._ack_pcm = pcm.tobytes()

    async def run(self) -> None:
        print("ATHENA is ready. Speak a command; press Ctrl+C to stop.")
        try:
            while not self.llm.shutdown_requested:
                if not self._external_speech.empty():
                    await self._stop_listening()
                    text = self._external_speech.get_nowait()
                    if self._external_speech.empty():
                        self._external_changed.clear()
                    try:
                        await self._speak_text(text)
                    finally:
                        self._external_speech.task_done()
                    continue
                if self._listen_task is None:
                    self.active_turn = uuid4()
                    self.voice_gate.reset()
                    self._listen_task = asyncio.create_task(self._listen(self.active_turn))
                # Inspect queues before clearing the wakeup event; no await between
                # them, so a completion cannot be lost.
                self.background.changed.clear()
                output = self.background.next_output()
                if output and self._safe_to_speak() and not self._listen_task.done():
                    job, acknowledgement = output
                    await self._deliver(job, acknowledgement)
                    continue
                changed = asyncio.create_task(self.background.changed.wait())
                external = asyncio.create_task(self._external_changed.wait())
                try:
                    await asyncio.wait((self._listen_task, changed, external),
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                    external.cancel()
                    await asyncio.gather(changed, external, return_exceptions=True)
                if self._external_changed.is_set():
                    continue
                if not self._listen_task.done():
                    continue
                transcript = await self._listen_task
                self._listen_task = None
                if not transcript:
                    continue
                if self.wake_word:
                    if time.monotonic() < self._active_until:
                        # After the wake word, accept normal speech for a short
                        # hands-free window. Refresh it after each utterance.
                        transcript = transcript.strip()
                        self._active_until = time.monotonic() + 20.0
                    else:
                        is_confirmation = self.background.is_confirmation_reply(transcript)
                        transcript = self._wake_command(transcript, allow_confirmation=is_confirmation)
                        if transcript is None:
                            print("Ignored: wake word not detected.", flush=True)
                            continue
                        self._active_until = time.monotonic() + 20.0
                        if not transcript:
                            await self._speak_text(random.choice(self.WAKE_ACKS))
                            continue
                if ToolRegistry.is_shutdown_command(transcript):
                    await self.background.cancel_all()
                    await self._answer(self.active_turn, transcript)
                    break
                command = ToolRegistry.normalize_command(transcript)
                if command in {"cancel background tasks", "cancel all tasks", "stop background tasks"}:
                    await self.background.cancel_all()
                    await self._speak_text("Background tasks cancelled.")
                elif command in {"background status", "task status", "what are you working on"}:
                    count = len(self.background.jobs)
                    await self._speak_text(f"I have {count} background request{'s' if count != 1 else ''} pending.")
                elif ToolRegistry.is_download_status_query(transcript):
                    await self._speak_text(self.background.download_status().spoken_text)
                elif self.background.submit(transcript, self.memory.context_messages()) is None:
                    await self._speak_text("I already have three requests pending. Let one finish, or say cancel background tasks.")
        finally:
            await self._stop_listening()
            await self.background.cancel_all()

    def _safe_to_speak(self):
        # Even the first few voiced frames reserve the floor for the user. Once
        # activated, wait for STT's final transcript, not a guessed short pause.
        return not self.voice_gate.active and self.voice_gate._consecutive_voiced == 0

    def _wake_command(self, text: str, *, allow_confirmation: bool = False) -> str | None:
        """Require ATHENA at the start of spoken commands before using DeepSeek."""
        if allow_confirmation:
            return text
        word = re.escape(self.wake_word)
        # STT commonly renders the name as two words; accept only these close
        # variants and only at the beginning, so room conversation is ignored.
        pattern = rf"^\s*(?:hey\s+)?(?:{word}|a\s+tina|a\s+thena|athina)\b[\s,:;.!?-]*(.*)$"
        match = re.match(pattern, text.casefold())
        return match.group(1).strip() if match else None

    async def _stop_listening(self):
        task, self._listen_task = self._listen_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _deliver(self, job, acknowledgement):
        # Cancel capture synchronously before starting speaker output. SDK stop
        # can be slow: finish it alongside playback, not before the first sound.
        task, self._listen_task = self._listen_task, None
        if task is not None:
            task.cancel()
        self.active_turn = uuid4()
        try:
            if acknowledgement:
                job.acknowledged = True
                print(f"A.T.H.E.N.A: On it. [background {str(job.id)[:8]}]")
                await self.speaker.play(self._ack_pcm)
            else:
                print(f"[Result for: {job.text[:100]}]")
                await self._speak_text(job.reply or "That request returned no answer.")
                self.background.delivered(job)
                await self.memory.remember_turn(job.id, job.text, job.reply or "No answer returned.")
        finally:
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    async def _speak_text(self, text):
        turn = uuid4()
        self.active_turn = turn
        self.state = AgentState.SPEAKING
        music_tool = self.llm._tools.get("netease_music")
        music_player = getattr(music_tool, "player", None)
        if music_player is not None:
            music_player.suspend_for_voice()
        print(f"A.T.H.E.N.A: {text}")
        playback = asyncio.create_task(self._play_audio(turn))
        try:
            async with asyncio.timeout(30):
                await self.tts.send_text(turn, text)
                await self.tts.flush(turn)
                await playback
        except TimeoutError:
            print("Speech output timed out; the answer is printed above.")
        except Exception:
            print("Speech output failed; the answer is printed above.")
        finally:
            if not playback.done():
                playback.cancel()
            await asyncio.gather(playback, return_exceptions=True)
            await self.tts.cancel(turn)
            self.state = AgentState.IDLE
            if music_player is not None:
                music_player.resume_after_voice()
            if self.wake_word and self._active_until > 0:
                # Give the user the full follow-up window after ATHENA finishes
                # speaking instead of consuming it during TTS or tool work.
                self._active_until = time.monotonic() + 20.0

    async def _listen(self, turn_id: UUID) -> str:
        self.state = AgentState.LISTENING
        self.settings_store.reload()
        self.voice_gate.configure(
            minimum_rms=self.settings_store.get("vad_minimum_rms"),
            noise_multiplier=self.settings_store.get("vad_noise_multiplier"),
            end_silence_ms=self.settings_store.get("vad_end_silence_ms"),
        )
        self.voice_gate.reset()
        async def capture() -> None:
            frames_seen = 0
            was_active = False
            pending_audio = bytearray()
            async for frame in self.microphone.frames():
                if self.active_turn != turn_id:
                    return
                for accepted_frame in self.voice_gate.process(frame):
                    pending_audio.extend(accepted_frame)
                    # Alibaba recommends roughly 100 ms per streaming packet.
                    # At 16 kHz, mono PCM16, that is 3,200 bytes.
                    while len(pending_audio) >= 3200:
                        await self.stt.send_audio(bytes(pending_audio[:3200]))
                        del pending_audio[:3200]
                frames_seen += 1
                if frames_seen % 5 == 0 or self.voice_gate.active != was_active:
                    self.audio_status.update(
                        rms=self.voice_gate.last_rms,
                        noise=self.voice_gate.noise_rms,
                        threshold=self.voice_gate.threshold,
                        speech=self.voice_gate.active,
                        voiced_frames=self.voice_gate.voiced_frames,
                    )
                if self.audio_debug and (frames_seen % 50 == 0 or self.voice_gate.active != was_active):
                    print(
                        "[audio] "
                        f"rms={self.voice_gate.last_rms:.0f} "
                        f"noise={self.voice_gate.noise_rms:.0f} "
                        f"threshold={self.voice_gate.threshold:.0f} "
                        f"speech={'yes' if self.voice_gate.active else 'no'} "
                        f"voiced_frames={self.voice_gate.voiced_frames}",
                        flush=True,
                    )
                was_active = self.voice_gate.active
                if self.voice_gate.should_end:
                    # Commit locally as soon as the silence window closes. Waiting
                    # only for server punctuation can add seconds or never finish.
                    if pending_audio:
                        await self.stt.send_audio(bytes(pending_audio))
                    await self.stt.finish_turn()
                    return

        capture_task = None
        try:
            await self.stt.start_turn(turn_id)
            print("\nListening...")
            capture_task = asyncio.create_task(capture())
            async for result in self.stt.results():
                if result.turn_id != turn_id:
                    continue
                if result.is_final and result.text == "[STT complete]":
                    return ""
                label = "You" if result.is_final else "..."
                print(f"{label}: {result.text}")
                if result.is_final:
                    if result.text.startswith("[STT error]"):
                        print("Speech recognition failed; listening again.")
                        return ""
                    if not self._accept_transcript(result.text):
                        print("Ignored: sound was too short to be speech.")
                        self.voice_gate.reset()
                        continue
                    return result.text
        finally:
            if capture_task is not None:
                capture_task.cancel()
                await asyncio.gather(capture_task, return_exceptions=True)
            await self.stt.finish_turn()
        return ""

    def _accept_transcript(self, text):
        return self.voice_gate.has_enough_speech or (
            (self.background.is_confirmation_reply(text) or self.llm.is_confirmation_reply(text)) and self.voice_gate.active
            and self.voice_gate.voiced_frames >= self.voice_gate.start_frames)

    async def _answer(self, turn_id: UUID, transcript: str) -> None:
        self.state = AgentState.THINKING
        chunker = SpeechChunker()
        playback_task = asyncio.create_task(self._play_audio(turn_id))
        response_parts: list[str] = []
        sent_audio = False
        print("A.T.H.E.N.A: ", end="", flush=True)
        try:
            context = self.memory.context_messages()
            async for fragment in self.llm.stream_reply(turn_id, transcript, context):
                if self.active_turn != turn_id:
                    return
                response_parts.append(fragment)
                print(fragment, end="", flush=True)
                for clause in chunker.feed(fragment):
                    self.state = AgentState.SPEAKING
                    await self.tts.send_text(turn_id, clause)
                    sent_audio = True
            for clause in chunker.finish():
                await self.tts.send_text(turn_id, clause)
                sent_audio = True
            print()
            if sent_audio:
                await self.tts.flush(turn_id)
                await playback_task
            assistant_text = "".join(response_parts).strip()
            if assistant_text:
                await self.memory.remember_turn(turn_id, transcript, assistant_text)
        finally:
            if not playback_task.done():
                playback_task.cancel()
                await asyncio.gather(playback_task, return_exceptions=True)
            self.state = AgentState.IDLE

    async def _play_audio(self, turn_id: UUID) -> None:
        async for chunk in self.tts.audio(turn_id):
            if chunk.turn_id != turn_id or self.active_turn != turn_id:
                continue
            await self.speaker.play(chunk.pcm)

    async def cancel_active_turn(self, reason: str = "interrupted") -> None:
        if self.active_turn is None:
            return
        self.state = AgentState.CANCELLING
        turn_id, self.active_turn = self.active_turn, None
        await self.speaker.stop()
        await asyncio.gather(
            self.tts.cancel(turn_id),
            self.llm.cancel(turn_id),
            return_exceptions=True,
        )
        print(f"Turn cancelled: {reason}")
        self.state = AgentState.IDLE

    async def close(self) -> None:
        await self._stop_listening()
        await self.background.cancel_all()
        music = self.llm._tools.get("netease_music")
        player = getattr(music, "player", None)
        if player is not None:
            await player.stop()
        await self.cancel_active_turn("shutdown")
        await asyncio.gather(
            self.stt.close(),
            self.tts.close(),
            self.llm.close(),
            self.memory.close(),
            self.microphone.close(),
            self.speaker.close(),
            return_exceptions=True,
        )
