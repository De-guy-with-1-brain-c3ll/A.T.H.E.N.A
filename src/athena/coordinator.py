from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from collections import OrderedDict, deque
from datetime import datetime
import os
import random
import re
import time
from uuid import UUID, uuid4

from athena.audio.capture import Microphone
from athena.audio.playback import Speaker
from athena.audio.vad import VoiceGate
from athena.audio.telemetry import AudioStatusWriter, LISTENING, SPEECH, WAITING
from athena.llm.deepseek import DeepSeekLanguageModel, DeepSeekUnavailable
from athena.llm.speech_chunker import SpeechChunker
from athena.memory.service import MemoryService
from athena.state import AgentState
from athena.settings.store import RuntimeSettingsStore
from athena.sleep import SleepRunner, status_report
from athena.stt.base import SpeechRecognizer
from athena.tts import speech_cost_label, speech_is_billed
from athena.tts.qwen import QwenRealtimeSynthesizer
from athena.background import BackgroundAgents
from athena.tools.registry import ToolRegistry


# How long synthesis may take before a turn gives up on it. Playback has its own
# much larger ceiling; sharing one clock between them cut long answers off.
# How many recently spoken replies to keep for free replay.
SPEECH_CACHE_ENTRIES = 24

SYNTHESIS_TIMEOUT_SECONDS = 30.0
PLAYBACK_TIMEOUT_SECONDS = 300.0


def synthesis_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("ATHENA_TTS_SYNTH_TIMEOUT", "30")))
    except ValueError:
        return SYNTHESIS_TIMEOUT_SECONDS


def speech_budget_from_environment() -> int:
    """How many characters may be spoken before synthesis stops.

    Off by default. Truncating speech was worse than any saving it made: a
    briefing that stops halfway is the behaviour that gets complained about. Set
    ATHENA_TTS_MAX_CHARS to a positive number to re-enable a runaway guard, and
    when it trips it says so in the log rather than going quiet mid-answer.
    """
    try:
        return max(0, int(os.environ.get("ATHENA_TTS_MAX_CHARS", "0")))
    except ValueError:
        return 0


def segment_cap_bytes() -> int:
    """Upper bound on audio streamed into one recognition segment.

    16 kHz, 16-bit mono is 32 KB per second. Music, television or a fan can hold
    the energy gate open for minutes at a stretch, and every streamed second is
    billed — a whole afternoon of background noise once turned into 47,600
    billed seconds in a single day. The cap also forces the recogniser to
    finalise, so a segment that never goes quiet still produces an answer.
    """
    try:
        seconds = float(os.environ.get("ATHENA_STT_SEGMENT_CAP_SECONDS", "20"))
    except ValueError:
        seconds = 20.0
    return max(2.0, seconds) * 32_000


def listen_while_music() -> bool:
    """Whether new speech segments start while music is playing.

    The gate cannot tell a lyric from a command, so by default the microphone
    stops starting recognition segments during playback: the song would be
    streamed, transcribed and answered to. Set ATHENA_LISTEN_WHILE_MUSIC=1 to
    accept the billing and the phantom commands.
    """
    return os.environ.get("ATHENA_LISTEN_WHILE_MUSIC", "0").strip().casefold() \
        in {"1", "true", "yes", "on"}


def end_of_speech_tone(rate: int) -> bytes:
    """The sound of the turn closing: two short falling notes, 880 then 587 Hz.

    The acknowledgement chirp rises. This one falls, so "I heard you" and "I
    have stopped listening" can never be mistaken for one another.
    """
    from array import array
    import math

    attack = max(1, int(rate * 0.008))
    release = max(1, int(rate * 0.03))

    def note(frequency: int, length: int) -> list[int]:
        return [
            int(2400 * min(1.0, i / attack, (length - i) / release)
                * math.sin(2 * math.pi * frequency * i / rate))
            for i in range(length)
        ]

    pcm = array("h", note(880, int(rate * 0.07))
                + [0] * int(rate * 0.015)
                + note(587, int(rate * 0.10)))
    return pcm.tobytes()


class VoiceCoordinator:
    WAKE_ACKS = ("Yes, sir?", "What's up?", "I'm listening.", "Go ahead.",
                 "Ready.", "I'm here.", "How can I help?", "At your service.",
                 "I'm listening, Benjamin.", "What do you need?")
    # A command repeated seconds later is nearly always the user talking over a
    # slow answer, not a second request. Answering it twice sounds broken; one
    # free acknowledgement keeps him informed without another model call. If he
    # says it a third time he means it, and it is honoured for real.
    REPEAT_WINDOW_SECONDS = 30.0
    REPEAT_ACKS = ("I heard you the first time, sir.", "Just answered that, sir.",
                   "You've said that, sir.")
    def __init__(
        self,
        microphone: Microphone,
        speaker: Speaker,
        # Either cloud or local: both satisfy the same protocol, and which one is
        # in use is chosen at start-up by ATHENA_STT_BACKEND.
        stt: SpeechRecognizer,
        llm: DeepSeekLanguageModel,
        tts: QwenRealtimeSynthesizer,
        memory: MemoryService,
        voice_gate: VoiceGate,
        settings_store: RuntimeSettingsStore,
        audio_debug: bool = False,
        local_wake_stt=None,
        interruption_stt=None,
    ) -> None:
        self.microphone = microphone
        self.speaker = speaker
        self.stt = stt
        self.interruption_stt = interruption_stt
        # Optional local SenseVoice gate. When present, cloud STT is not opened
        # until this local decoder finds an utterance containing the wake word.
        self.local_wake_stt = local_wake_stt
        self.llm = llm
        self.tts = tts
        self.memory = memory
        self.voice_gate = voice_gate
        self.settings_store = settings_store
        self.audio_debug = audio_debug
        self.audio_status = AudioStatusWriter.from_environment()
        self.state = AgentState.IDLE
        self._sleep_task: asyncio.Task | None = None
        self._sleep_report: str | None = None
        # The pass itself, kept across calls so the status record and the model
        # client survive: a sleep job that finishes after the turn returns has to
        # still be able to say what it did.
        self.sleep = SleepRunner()
        # Set by the interface wiring, like alerts.notify. Used to offer a brief
        # that a scheduled watcher prepared while nobody was around.
        self.alerts = None
        self._brief_offer: str | None = None
        self.speech_budget = speech_budget_from_environment()
        # Recently spoken audio, so an exact repeat costs nothing.
        self._speech_cache: OrderedDict[str, bytes] = OrderedDict()
        self.active_turn: UUID | None = None
        self.background = BackgroundAgents(llm)
        self._ack_pcm = b""
        self._eos_pcm = b""
        self._alarm_pcm = b""
        self.eos_tone = os.environ.get("ATHENA_EOS_TONE", "1").strip().lower() not in {
            "0", "false", "no", "off"}
        self._warm_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._external_speech: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
        self._audio_switch_requests = asyncio.Queue(maxsize=2)
        self._external_changed = asyncio.Event()
        # Desktop/testing stays backwards-compatible; the Orange Pi env enables
        # this for its always-on microphone service.
        self.wake_word = os.environ.get("ATHENA_WAKE_WORD", "").strip().casefold()
        self._active_until = 0.0
        # Repeat detection, see REPEAT_WINDOW_SECONDS above.
        self._last_command_text: str | None = None
        self._last_command_at = 0.0
        self._repeat_acknowledged = False
        # The dashboard can move the speaker level. Kept here as the one source
        # of truth so a restart re-applies the level the user last chose.
        try:
            self._volume = max(0, min(100, int(os.environ.get("ATHENA_VOLUME", "100"))))
        except ValueError:
            self._volume = 100

    def enqueue_external_speech(self, text: str) -> bool:
        try:
            self._external_speech.put_nowait(text)
            self._external_changed.set()
            return True
        except asyncio.QueueFull:
            # Dropping it silently meant an alarm could simply never ring, with
            # nothing anywhere to explain the silence.
            print(f"[dropped, speech queue full] {text[:120]}", flush=True)
            return False

    async def request_audio_route(self, target="status"):
        router = getattr(self, "audio_router", None)
        if router is None: raise RuntimeError("Live audio switching is unavailable on this instance.")
        if target == "status": return router.status()
        if target not in {"computer", "pi"}: raise ValueError("Choose pi or computer.")
        future = asyncio.get_running_loop().create_future()
        try: self._audio_switch_requests.put_nowait((target, future))
        except asyncio.QueueFull: raise RuntimeError("An audio switch is already pending.") from None
        self._external_changed.set()
        return await asyncio.wait_for(future, 10)

    async def _switch_audio(self, target):
        router = getattr(self, "audio_router", None)
        if router is None: raise RuntimeError("Live audio switching is unavailable on this instance.")
        await self._stop_listening()
        async with self._audio_focus():
            result = await router.switch(target)
        await self.set_volume(self._volume)
        self.voice_gate.reset()
        self._active_until = time.monotonic() + 20
        return result

    async def _handle_fast_audio_control(self, text):
        command = ToolRegistry.normalize_command(text)
        if not re.search(r"\b(?:switch|move|use|route|hand|go back|back to)\b", command): return False
        audio_named = re.search(r"\b(?:audio|speaker|microphone|mic|input|output|devices|listening|pi)\b", command)
        returning = re.search(r"\b(?:switch|move|route|hand)\s+(?:it\s+|them\s+)?back\b|\b(?:switch|move|route)\s+(?:it|them)\s+to\b", command)
        if not audio_named and not returning: return False
        destinations = re.findall(r"\b(?:to|on|use)\s+(?:(?:the|my|your)\s+)?(computer|pc|laptop|desktop|pi|board)\b", command)
        named = destinations[-1] if destinations else None
        target = ("pi" if named in {"pi", "board"} else "computer") if named else "computer" if re.search(r"\b(?:computer|pc|laptop|desktop)\b", command) else "pi" if re.search(r"\b(?:pi|board)\b", command) else None
        if target is None: return False
        try:
            await self._switch_audio(target)
            await self._speak_text("Microphone and speaker switched to your " + ("computer." if target == "computer" else "Pi."), prompted=True)
        except (RuntimeError, OSError, ValueError) as error:
            await self._speak_text(str(error), prompted=True)
        return True

    @staticmethod
    def _music_state(player) -> dict:
        """The player's status, with the saved playlists alongside it.

        One response shape for every music call, so the dashboard can render the
        playlists without tracking which action it happened to ask for — and so
        a playlist added from the panel appears in the very next poll.
        """
        state = player.status()
        listing = getattr(player, "playlists", None)
        if callable(listing):
            state = {**state, "playlists": listing()}
        return state

    async def control_music(self, action: str, value: int | None = None,
                            name: str | None = None, query: str = "",
                            moods: list[str] | None = None) -> dict:
        tool = self.llm._tools.get("netease_music")
        player = getattr(tool, "player", None)
        if player is None:
            raise RuntimeError("Music is unavailable while ATHENA voice is offline.")
        message = ""
        if action == "status":
            return self._music_state(player)
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
        elif action == "add_playlist":
            saved = player.add_playlist(str(name or ""), str(query or ""), moods or [])
            message = f"Saved the {saved} playlist."
        elif action == "remove_playlist":
            removed = player.remove_playlist(str(name or ""))
            message = f"Removed the {removed} playlist."
        elif action in {"play_playlist", "auto_play"}:
            if action == "play_playlist":
                found = player._named_playlist(str(name or ""))
                if found is None:
                    raise RuntimeError("I don't have that playlist. Ask me to list your saved playlists.")
                chosen, entry = found
            else:
                chosen, entry = player.choose_playlist(str(query or ""))
            await player.play(entry["query"])
            message = f"Playing the {chosen} playlist."
        else:
            raise RuntimeError("Unknown music command.")
        return {**self._music_state(player), "message": message}

    @property
    def volume(self) -> int:
        """The speaker level, preferring the speaker's own view when it has one."""
        getter = getattr(self.speaker, "volume", None)
        if isinstance(getter, int):
            return getter
        return self._volume

    async def set_volume(self, percent: int) -> int:
        """Move the speaker level and remember it. Returns the level in force."""
        value = max(0, min(100, int(percent)))
        self._volume = value
        setter = getattr(self.speaker, "set_volume", None)
        if setter is not None:
            setter(value)
        return value

    async def connect(self) -> None:
        connections = [
            self.microphone.open(),
            self.speaker.open(),
            self.stt.connect(),
            *([self.interruption_stt.connect()] if self.interruption_stt is not None else []),
            self.llm.connect(),
            self.tts.connect(),
            self.memory.connect(),
        ]
        if self.local_wake_stt is not None:
            connections.append(self.local_wake_stt.connect())
        await asyncio.gather(*connections)
        # Re-apply the remembered level now that the speaker owns a stream.
        await self.set_volume(self._volume)
        # Piper's voice takes most of two seconds to load, and it is the local
        # backend's only real drawback. Start it now, in the background, so the
        # first reply does not wait for it. Deliberately not awaited: a slower
        # start must never be traded for a later one, and a backend without a
        # warm-up (the cloud voice) simply does not have the method.
        warm = getattr(self.tts, "warm", None)
        if warm is not None:
            self._warm_task = asyncio.create_task(warm())
        # Keep acknowledgement and alarm audio local. This avoids extra TTS/API
        # work and makes wake recognition feel immediate.
        from array import array
        import math
        import sys
        rate = int(getattr(self.speaker, "_sample_rate", 24000) or 24000)

        def sample_bytes(samples):
            pcm = array("h", samples)
            if sys.byteorder != "little":
                pcm.byteswap()
            return pcm.tobytes()

        chirp_len = int(rate * 0.18)
        self._ack_pcm = sample_bytes(
            int(2600 * (0.72 * math.sin(2 * math.pi * (460 + 900 * i / chirp_len) * i / rate)
                        + 0.28 * math.sin(2 * math.pi * (920 + 1800 * i / chirp_len) * i / rate))
                * min(1.0, i / (rate * 0.015), (chirp_len - i) / (rate * 0.06)))
            for i in range(chirp_len)
        )

        alarm_len = rate * 10
        def alarm_sample(i):
            phase = i % rate
            if phase >= int(rate * 0.12):
                return 0
            envelope = min(1.0, phase / (rate * 0.008), (rate * 0.12 - phase) / (rate * 0.025))
            return int(2400 * envelope * math.sin(2 * math.pi * 880 * phase / rate))
        self._alarm_pcm = sample_bytes(alarm_sample(i) for i in range(alarm_len))

        # A falling pair, the mirror of the acknowledgement chirp, so "I heard
        # you" and "I have stopped listening" cannot be confused.
        self._eos_pcm = end_of_speech_tone(rate)

    async def run(self) -> None:
        print("ATHENA is ready. Speak a command; press Ctrl+C to stop.")
        try:
            while not self.llm.shutdown_requested:
                audio_requests = getattr(self, "_audio_switch_requests", None)
                if audio_requests is not None and not audio_requests.empty():
                    target, future = self._audio_switch_requests.get_nowait()
                    if not future.done():
                        try:
                            result = await self._switch_audio(target)
                            if not future.done(): future.set_result(result)
                        except Exception as error:
                            if not future.done(): future.set_exception(error)
                    self._audio_switch_requests.task_done()
                    self._external_changed.clear()
                    continue
                if not self._external_speech.empty() and (not self.background.jobs and self._safe_to_speak()):
                    await self._stop_listening()
                    text = self._external_speech.get_nowait()
                    if self._external_speech.empty():
                        self._external_changed.clear()
                    try:
                        if text.casefold().startswith("alarm:"):
                            # An alarm the user set always rings, whatever the hour.
                            await self._speak_text(text)
                            await self._play_short_sound(self._alarm_pcm)
                        elif self.quiet_hours():
                            # Nothing unprompted is spoken overnight. It is not
                            # lost: it is printed here and shown on the dashboard.
                            print(f"[quiet hours, not spoken] {text}", flush=True)
                        else:
                            await self._speak_text(text)
                    finally:
                        self._external_speech.task_done()
                    continue
                if not self._external_speech.empty():
                    # Reports wait for the conversational floor, never cancel
                    # capture or talk over a pending answer.
                    self._external_changed.clear()
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
                    try:
                        await self._deliver(job, acknowledgement)
                    except Exception as error:
                        print(f"Delivery failed ({type(error).__name__}); continuing.", flush=True)
                    continue
                changed = asyncio.create_task(self.background.changed.wait())
                external = asyncio.create_task(self._external_changed.wait())
                watched = [self._listen_task, changed, external]
                if self._sleeping:
                    watched.append(self._sleep_task)
                try:
                    await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                    external.cancel()
                    await asyncio.gather(changed, external, return_exceptions=True)
                if self._sleep_task is not None and self._sleep_task.done():
                    await self._announce_sleep_result()
                    continue
                if self._external_changed.is_set():
                    continue
                if not self._listen_task.done():
                    continue
                try:
                    transcript = await self._listen_task
                except Exception as error:
                    # A speech-recognition failure ends one listen, not the service.
                    print(f"Listening failed ({type(error).__name__}); listening again.", flush=True)
                    transcript = ""
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
                        verified = getattr(self, "_keyword_verified_turn", None) == self.active_turn
                        command = self._wake_command(transcript, allow_confirmation=is_confirmation)
                        transcript = transcript.strip() if command is None and verified else command
                        if transcript is None:
                            print("Ignored: wake word not detected.", flush=True)
                            continue
                        self._active_until = time.monotonic() + 20.0
                        await self._play_short_sound(self._ack_pcm)
                        if self._sleeping:
                            # Saying the wake word is how sleep mode is left.
                            await self._wake_from_sleep()
                            if not transcript:
                                continue
                        if not transcript:
                            await self._speak_text(random.choice(self.WAKE_ACKS), prompted=True)
                            continue
                if await self._handle_quiet_request(transcript):
                    continue
                if ToolRegistry.is_shutdown_command(transcript):
                    await self.background.cancel_all()
                    await self._answer(self.active_turn, transcript)
                    break
                if self._brief_offer is not None and not self.background.is_confirmation_reply(transcript):
                    key, self._brief_offer = self._brief_offer, None
                    if self._is_affirmative(transcript) and self.alerts is not None:
                        brief = self.alerts.take_brief(key)
                        if brief:
                            await self._speak_text(brief["text"])
                            continue
                command = ToolRegistry.normalize_command(transcript)
                if command in {"cancel background tasks", "cancel all tasks", "stop background tasks"}:
                    await self.background.cancel_all()
                    await self._speak_text("Background tasks cancelled.")
                elif command in {"background status", "task status", "what are you working on"}:
                    count = len(self.background.jobs)
                    pending = f"I have {count} background request{'s' if count != 1 else ''} pending."
                    summary = getattr(getattr(self.llm, '_tools', None), 'background_summary', None)
                    if callable(summary):
                        details = summary()
                        if isinstance(details, str):
                            pending += ' ' + details
                    if self._sleeping:
                        awaiting = status_report().replace("\n", " ")
                        pending += f" Also {awaiting[0].lower()}{awaiting[1:]}"
                    await self._speak_text(pending)
                elif ToolRegistry.is_download_status_query(transcript):
                    await self._speak_text(self.background.download_status().spoken_text)
                elif await self._handle_fast_audio_control(transcript):
                    pass
                elif await self._handle_browser_status(transcript):
                    pass
                elif self.is_memory_status_query(transcript):
                    # Answered from the shared record, so it stays correct when the
                    # pass was started by the dashboard or a scheduled job rather
                    # than by this interface.
                    await self._speak_text(status_report())
                elif self.is_sleep_command(transcript):
                    await self._enter_sleep()
                elif await self._handle_fast_music_control(transcript):
                    pass
                elif ToolRegistry.is_task_status_query(transcript) and (execution_status := self.background.contextual_status(transcript, self._voice_context())) is not None:
                    await self._speak_text(execution_status.spoken_text)
                elif self._is_repeat(transcript):
                    await self._speak_text(random.choice(self.REPEAT_ACKS))
                else:
                    job = self.background.submit(transcript, self._voice_context())
                    if job is None:
                        await self._speak_text("I already have three requests pending. Let one finish, or say cancel background tasks.")
                    else:
                        if hasattr(job, 'speech_eos_at'):
                            job.speech_eos_at = getattr(getattr(self, 'audio_status', None), '_payload', {}).get('eos_at')
                        self._remember_command(transcript)
                    continue  # Offer memos after the actual background reply, not submission.
                if not self.background.is_confirmation_reply("yes"):
                    await self._offer_brief()
        finally:
            await self._stop_listening()
            await self.background.cancel_all()

    AFFIRMATIVES = {
        "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go on", "go ahead",
        "do it", "read it", "read it out", "read it to me", "please do",
        "yes please", "yes please do", "tell me", "give it to me", "go",
    }

    @classmethod
    def _is_affirmative(cls, text: str) -> bool:
        """Whether a reply means yes, without borrowing the approval predicate."""
        return ToolRegistry.normalize_command(text) in cls.AFFIRMATIVES

    async def _send_clause(self, turn_id: UUID, clause: str) -> None:
        """Hand one clause to synthesis, bounded.

        `send_text` only enqueues — the synthesizer's pump owns the waiting —
        but it can still block briefly on a supersede, which stops the previous
        turn's pump and decoder before starting this one. Unbounded, a wedged
        teardown would leave the turn in SPEAKING forever: no audio, no return
        to listening, and the "answer is printed above" handler unreachable
        because the stream never ended.
        """
        async with asyncio.timeout(synthesis_timeout()):
            await self.tts.send_text(turn_id, clause)

    def _budget_reached(self, spoken: int) -> bool:
        """Only a configured positive budget can stop speech early."""
        return self.speech_budget > 0 and spoken >= self.speech_budget

    def quiet_hours(self) -> bool:
        """True when ATHENA must not speak unless it was asked something.

        A scheduled watcher or a finished background job has no idea what time it
        is. Speaking a failure at 3am is worse than not reporting it, so outside
        the waking window those messages are printed and shown on the dashboard
        instead of spoken. An alarm is exempt: ringing is the entire point.
        """
        raw = os.environ.get("ATHENA_QUIET_HOURS", "22:00-07:00").strip()
        match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", raw)
        if not match:
            return False
        start = int(match.group(1)) * 60 + int(match.group(2))
        end = int(match.group(3)) * 60 + int(match.group(4))
        if start == end:
            return False
        zone = os.environ.get("ATHENA_TIMEZONE", "Asia/Shanghai")
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(zone))
        except Exception:
            now = datetime.now()
        minutes = now.hour * 60 + now.minute
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end   # crosses midnight

    def _safe_to_speak(self):
        # Even the first few voiced frames reserve the floor for the user. Once
        # activated, wait for STT's final transcript, not a guessed short pause.
        return not self.voice_gate.hearing_speech

    # A question about memory, in the words actually used, matched locally.
    # These used to be routed only to the model, which had no consolidation state
    # and answered about sleep mode as if the tool were the real thing.
    STALE_MEMORY_PATTERN = re.compile(
        r"\b(?:consolidat\w*|synced|up\s+to\s+date|behind)\b.*\b(?:memory|memories|recall)\b"
        r"|\b(?:memory|memories)\b.*\b(?:consolidat\w*|up\s+to\s+date|behind|saved|updated|synced)\b"
        r"|\b(?:when|did)\b.*\b(?:last|save|saved|consolidate|consolidated)\b.*\b(?:memory|memories|today)\b"
        r"|\bhow\s+did\s+(?:the\s+)?(?:consolidation|sleep|sleep\s+mode)\s+go\b"
        r"|\b(?:consolidation|sleep\s+mode|memory\s+consolidation)\s+(?:status|progress)\b"
        r"|\bstatus\s+of\s+(?:your\s+)?(?:memory|consolidation)\b")

    @classmethod
    def is_memory_status_query(cls, text: str) -> bool:
        return bool(cls.STALE_MEMORY_PATTERN.search(ToolRegistry.normalize_command(text)))

    async def _offer_brief(self) -> bool:
        """Ask about a prepared brief. Returns True when one was offered.

        A scheduled watcher builds the brief and stays quiet, because at four in
        the afternoon there may be nobody in the room. This is the moment he is
        actually there, so this is where it gets offered.
        """
        if getattr(self, "alerts", None) is None or getattr(self, "_brief_offer", None) is not None:
            return False
        if getattr(getattr(self, "background", None), "jobs", {}):
            return False
        try:
            briefs = self.alerts.brief_rows()
        except Exception:
            return False
        if not briefs:
            return False
        offered = getattr(self, "_offered_briefs", set())
        fresh = []
        for brief in briefs:
            identity = (brief["key"], brief.get("ready_at", ""))
            if identity in offered:
                continue
            try:
                ready = datetime.fromisoformat(brief["ready_at"])
                if (datetime.now(ready.tzinfo) - ready).total_seconds() > 86400:
                    continue
            except (KeyError, ValueError, TypeError):
                pass
            fresh.append(brief)
        if not fresh:
            return False
        briefs = fresh
        if self.quiet_hours():
            print(f"[quiet hours, not spoken] a brief is ready: {briefs[0]['label']}",
                  flush=True)
            return False
        self._brief_offer = briefs[0]["key"]
        offered.add((briefs[0]["key"], briefs[0].get("ready_at", "")))
        self._offered_briefs = offered
        await self._speak_text(f"I've got {briefs[0]['label']}. Want it?")
        return True

    async def _handle_browser_status(self, text: str) -> bool:
        command = ToolRegistry.normalize_command(text)
        if not re.search(r"\b(?:did you open|is it open|is it opened|have you opened|browser status)\b", command):
            return False
        from athena.tools.pc_browser import PCBrowserTool
        result = await PCBrowserTool().execute({"action": "status"})
        await self._speak_text(result.spoken_text, prompted=True)
        return True

    # ---- Sleep mode ---------------------------------------------------------
    #
    # Sleep mode consolidates the day's short-term conversation into long-term
    # memory with the stronger model. It runs in the background rather than
    # blocking the tool call, so ATHENA keeps listening throughout and the wake
    # word is what brings it back. Nothing is written until the very end, so
    # waking early loses no partial work.

    SLEEP_COMMANDS = {
        "go to sleep", "sleep", "sleep mode", "sleep on it", "go to sleep please",
        "remember today", "remember the day", "consolidate memory",
        "consolidate your memory", "consolidate the day",
    }

    # What the model's own sleep tool says to the user. Spoken here rather than by
    # the model, because the model never sees the background job's result: it only
    # knows that the pass started.
    SLEEP_START_REPLY = ("Going to sleep. I'll consolidate today in the background — "
                         "ask me how it went, or say my name to wake me.")

    @classmethod
    def is_sleep_command(cls, text: str) -> bool:
        return ToolRegistry.normalize_command(text) in cls.SLEEP_COMMANDS

    @property
    def _sleeping(self) -> bool:
        return self._sleep_task is not None and not self._sleep_task.done()

    async def _enter_sleep(self) -> None:
        """Voice sleep mode: same pass as the tool, plus the sleeping state."""
        started = await self.start_sleep()
        if not started.success:
            # "Already consolidating" is not a reason to claim to be asleep.
            await self._speak_text("Already asleep." if self._sleeping else started.spoken_text)
            return
        self.state = AgentState.SLEEPING
        await self._speak_text(self.SLEEP_START_REPLY)

    async def start_sleep(self, day=None):
        """Start a background pass, whoever asked for it."""
        from athena.tools.models import ToolResult

        if self._sleeping or self.sleep.is_running():
            return ToolResult(False, "Already consolidating memory.")
        self._sleep_report = None
        self._sleep_task = asyncio.create_task(self._consolidate(day))
        return ToolResult(True, self.SLEEP_START_REPLY)

    async def _consolidate(self, day=None) -> None:
        try:
            report = await self.sleep.consolidate(day)
            self._sleep_report = report.describe()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._sleep_report = f"Sleep mode could not finish: {error}"

    async def _wake_from_sleep(self) -> None:
        """Leave sleep mode, abandoning a consolidation that has not written yet."""
        task, self._sleep_task = self._sleep_task, None
        self._sleep_report = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.state = AgentState.LISTENING
        await self._speak_text("Awake.")

    async def _announce_sleep_result(self) -> None:
        report, self._sleep_report = self._sleep_report, None
        self._sleep_task = None
        if self.state == AgentState.SLEEPING:
            self.state = AgentState.IDLE
        if report is None:
            return
        # Never spoken. A pass normally runs at night, and it used to wake him up
        # to announce itself. Asking "how did it go" is answered from the status
        # record instead, and what it produced is offered the next morning.
        print(report, flush=True)
        self._queue_sleep_brief(report)

    def _queue_sleep_brief(self, report: str) -> None:
        """Add what the pass produced to the day's brief, silently.

        The brief is the mechanism for exactly this: something is ready, nobody is
        listening right now, offer it when they are.
        """
        if self.alerts is None:
            return
        try:
            self.alerts.save_brief("memory", "a memory update",
                                   "Memory consolidation finished.\n\n" + report)
        except Exception as error:
            print(f"Could not queue the memory brief ({type(error).__name__}).", flush=True)


    def _voice_context(self):
        """Use compact memory on the real service, while keeping test/plug-in
        memory providers that expose the old no-argument method compatible."""
        try:
            return self.memory.context_messages(max_turns=3, max_chars=2800)
        except TypeError:
            return self.memory.context_messages()

    def _wake_command(self, text: str, *, allow_confirmation: bool = False) -> str | None:
        """Require ATHENA at the start of spoken commands before using DeepSeek."""
        if allow_confirmation:
            return text
        word = re.escape(self.wake_word)
        # STT commonly renders the name as two words; accept only these close
        # variants and only at the beginning, so room conversation is ignored.
        pattern = rf"^\s*(?:hey\s+)?(?:{word}|a\s+tina|a\s+thena|athina|atheina|ethena)\b[\s,:;.!?-]*(.*)$"
        match = re.match(pattern, text.casefold())
        return match.group(1).strip() if match else None

    async def _handle_quiet_request(self, text: str) -> bool:
        command = re.sub(r"[^a-z ]", "", text.casefold()).strip()
        if command not in {"thats enough for now", "go quiet", "stop listening", "standby"}:
            return False
        await self._speak_text("Very well.")
        self._active_until = 0.0
        return True

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
                await self._play_short_sound(self._ack_pcm)
            else:
                print(f"[Result for: {job.text[:100]}]")
                if job.reply_started:
                    # The model is still streaming.  Start speech now rather
                    # than waiting for the last token to arrive; this is the
                    # critical path for a natural voice response.
                    job.delivery_started = True
                    self._delivery_eos_at = getattr(job, 'speech_eos_at', None)
                    self._delivery_first_text_at = getattr(job, 'first_text_at', None)
                    await self._answer_stream(self.active_turn, job.text,
                                              self.background.reply_stream(job))
                else:
                    # A failed/empty stream has no fragments to drain, but it
                    # still deserves the existing clear fallback.
                    await self._speak_text(job.reply or "That request returned no answer.",
                                           prompted=True)
                    await self.memory.remember_turn(
                        job.id, job.text, job.reply or "No answer returned.")
                self.background.delivered(job)
                if not getattr(self.background, "is_confirmation_reply", lambda _text: False)("yes"):
                    await self._offer_brief()
        finally:
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    @asynccontextmanager
    async def _audio_focus(self):
        # Every background audio source shares this one speaker, so all of them
        # have to yield while Athena talks. Ducking only the music player left
        # YouTube narrating over the reply.
        players = [getattr(self.llm._tools.get(name), "player", None)
                   for name in ("netease_music", "youtube_audio")]
        players = [player for player in players if player is not None]
        for player in players:
            player.suspend_for_voice()
        try:
            for player in players:
                settle = getattr(player, "wait_for_voice", None)
                if asyncio.iscoroutinefunction(settle):
                    await settle()
            yield
        finally:
            try:
                if asyncio.current_task().cancelling():
                    await self.speaker.stop()
                else:
                    finish = getattr(self.speaker, "finish_playback", None)
                    if asyncio.iscoroutinefunction(finish):
                        await finish()
            finally:
                for player in players:
                    player.resume_after_voice()

    async def _play_short_sound(self, pcm):
        async with self._audio_focus():
            await self.speaker.play(pcm)

    async def _handle_fast_music_control(self, text):
        command = ToolRegistry.normalize_command(text)
        actions = {"pause": "pause", "pause music": "pause", "pause the music": "pause",
                   "resume music": "resume", "resume the music": "resume", "resume": "resume",
                   "next": "next", "next track": "next", "next song": "next", "skip": "next", "skip track": "next",
                   "continue music": "resume", "continue": "resume",
                   "stop music": "stop", "stop the music": "stop"}
        action = actions.get(command)
        if not action:
            return False
        tools = getattr(self.llm, "_tools", None)
        tool = tools.get("netease_music") if tools is not None else None
        if getattr(tool, "player", None) is None:
            return False
        result = await tool.execute({"action": action})
        await self._speak_text(result.spoken_text, prompted=True)
        memory = getattr(self, "memory", None)
        if memory is not None:
            await memory.remember_turn(uuid4(), text, result.spoken_text)
        return True

    def _music_is_audible(self):
        tools = getattr(self.llm, "_tools", None)
        tool = tools.get("netease_music") if tools is not None else None
        status = getattr(getattr(tool, "player", None), "status", None)
        if not callable(status):
            return False
        try:
            state = status()
        except Exception:
            return False
        return isinstance(state, dict) and bool(state.get("playing")) and not state.get("paused", False)

    async def _speak_text(self, text, prompted: bool = False):
        async def speak():
            async with self._audio_focus():
                await self._speak_text_impl(text, prompted)
        await self._interruptible_speech(speak())
        if prompted and self.wake_word and self._active_until > 0:
            self._active_until = time.monotonic() + 20.0

    async def _speak_text_impl(self, text, prompted: bool = False):
        """Speak text. `prompted` means the user asked for this reply.

        Only a reply they asked for reopens the hands-free window. A watcher
        announcement used to re-arm it too, so for twenty seconds afterwards any
        conversation in the room was taken as a command and answered.
        """
        from athena.tts.text import speech_text
        text = speech_text(text)
        if not text:
            return
        turn = uuid4()
        self.active_turn = turn
        self.state = AgentState.SPEAKING
        print(f"A.T.H.E.N.A: {text}")
        if getattr(self.speaker, "can_speak_text", False):
            # The computer that is already listening has a voice of its own.
            # Letting it speak keeps synthesis, decoding and pacing off the
            # board completely, and removes a cloud round trip from every
            # reply. It only applies while a browser that claimed the
            # capability is actually connected.
            spoken = False
            try:
                print("[speech] spoken by the connected computer "
                      "(nothing synthesized on the board)", flush=True)
                spoken = await self.speaker.speak_text(text)
            except Exception:
                print("Speech output failed; the answer is printed above.")
            finally:
                self.state = AgentState.IDLE
                if prompted and self.wake_word and self._active_until > 0:
                    self._active_until = time.monotonic() + 20.0
            if spoken:
                return
            print("[speech] browser speech failed; trying synthesized audio.", flush=True)
            self.state = AgentState.SPEAKING
        cached = self._cached_speech(text)
        if cached:
            # Already spoken before, so it costs nothing to say again.
            print(f"[speech] replayed from cache ({len(cached)} bytes, no synthesis)",
                  flush=True)
            try:
                await self.speaker.play(cached)
            finally:
                self.state = AgentState.IDLE
            return
        collected: list[bytes] = []
        playback = asyncio.create_task(self._play_audio(turn, collected))
        try:
            # Bound synthesis only. This is the path a background job's answer
            # takes, and a channel roundup is well over half a minute of speech.
            # Sharing one clock with the playback cut it off mid-sentence while
            # the whole answer sat in the log — the exact complaint, twice,
            # because the same mistake was made in _answer as well.
            async with asyncio.timeout(synthesis_timeout()):
                await self.tts.send_text(turn, text)
                await self.tts.flush(turn)
            async with asyncio.timeout(PLAYBACK_TIMEOUT_SECONDS):
                played = await playback
            seconds = (played or 0) / 48_000
            # Speech is billed per 10,000 characters, but only the cloud voice is
            # billed at all. Showing a price after a reply spoken by the local
            # voice says the opposite of what happened.
            if speech_is_billed(self.tts):
                # Keep operational logs ASCII.  The Windows service console is
                # commonly configured with a legacy code page, where the yen
                # glyph raises UnicodeEncodeError *after* the audio has played.
                # That used to report a successful turn as failed and skip the
                # replay cache.
                cost = f" (CNY {len(text) / 10_000:.4f})"
            else:
                cost = f" ({speech_cost_label(self.tts)})"
            print(f"[speech] {len(text)} characters{cost}, "
                  f"{played or 0} bytes played (~{seconds:.1f}s)", flush=True)
            # Only keep it if the whole reply was played, never a fragment.
            if played and played == sum(len(part) for part in collected):
                self._remember_speech(text, b"".join(collected))
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
            if prompted and self.wake_word and self._active_until > 0:
                # Give the user the full follow-up window after ATHENA finishes
                # speaking instead of consuming it during TTS or tool work.
                self._active_until = time.monotonic() + 20.0

    async def _play_end_of_speech_tone(self) -> None:
        """Say out loud that the turn just closed."""
        pcm = getattr(self, "_eos_pcm", b"")
        if not pcm or not getattr(self, "eos_tone", False):
            return
        try:
            await self._play_short_sound(pcm)
        except Exception:
            print("End-of-speech tone failed to play; carrying on.", flush=True)

    async def _listen_local_wake(self, turn_id: UUID) -> str:
        """Reject background speech locally, but use Qwen for real commands."""
        self.state = AgentState.LISTENING
        self.settings_store.reload()
        self.voice_gate.configure(
            minimum_rms=self.settings_store.get("vad_minimum_rms"),
            noise_multiplier=self.settings_store.get("vad_noise_multiplier"),
            end_silence_ms=self.settings_store.get("vad_end_silence_ms"),
            start_ms=self.settings_store.get("vad_start_ms"),
            minimum_speech_ms=self.settings_store.get("vad_minimum_speech_ms"),
        )
        self.voice_gate.reset()
        self.audio_status.state(LISTENING, turn=str(turn_id))
        self.audio_status.heard("")
        chunks: list[bytes] = []
        total_bytes = 0
        frames_seen = 0
        cap = segment_cap_bytes()
        music_note_shown = False
        was_active = False

        def music_is_playing() -> bool:
            try:
                tool = self.llm._tools.get("netease_music")
                state = getattr(getattr(tool, "player", None), "status", lambda: {})()
                return isinstance(state, dict) and bool(state.get("playing")) and not state.get("paused", False)
            except Exception:
                return False

        try:
            print("Listening locally for the wake word...", flush=True)
            async for frame in self.microphone.frames():
                if self.active_turn != turn_id:
                    return ""
                accepted = self.voice_gate.process(frame)
                if (accepted and not listen_while_music() and music_is_playing()):
                    if not music_note_shown:
                        music_note_shown = True
                        print("[audio] music is playing; local wake detection paused", flush=True)
                    self.voice_gate.reset()
                    continue
                for packet in accepted:
                    chunks.append(packet)
                    total_bytes += len(packet)
                frames_seen += 1
                if frames_seen % 25 == 0 or self.voice_gate.active != was_active:
                    self.audio_status.update(
                        rms=self.voice_gate.last_rms,
                        noise=self.voice_gate.noise_rms,
                        threshold=self.voice_gate.threshold,
                        speech=self.voice_gate.active,
                        voiced_frames=self.voice_gate.voiced_frames,
                        retained_frames=len(chunks),
                        retained_seconds=len(chunks) * self.voice_gate.frame_ms / 1000.0,
                    )
                was_active = self.voice_gate.active
                if total_bytes >= cap or self.voice_gate.should_end:
                    break
            if not chunks or not self.voice_gate.has_enough_speech:
                return ""
            self.audio_status.endpoint(turn=str(turn_id), frames=len(chunks),
                                       seconds=total_bytes / 32_000)
            await self._play_end_of_speech_tone()
            text = await self.local_wake_stt.transcribe_once(b"".join(chunks))
            text = (text or "").strip()
            if not text:
                return ""
            # The local model is only a cheap gate. It is not the recognizer
            # whose words the conversation uses: keep the previous Qwen STT
            # behaviour for commands, follow-ups and approval replies.
            active = time.monotonic() < self._active_until
            confirmation = self.background.is_confirmation_reply(text)
            if self.wake_word and not (active or confirmation or self._wake_command(text) is not None):
                print("[audio] background speech rejected locally; no cloud audio sent.",
                      flush=True)
                return ""
            cloud_text = await self._transcribe_cloud_pcm(turn_id, b"".join(chunks))
            if not cloud_text:
                return ""
            print(f"You: {cloud_text}", flush=True)
            self.audio_status.transcript(cloud_text)
            return cloud_text
        finally:
            self.audio_status.state(WAITING)
            self.voice_gate.reset()

    async def _transcribe_cloud_pcm(self, turn_id: UUID, pcm: bytes) -> str:
        """Replay one locally approved utterance to the normal cloud STT."""
        await self.stt.start_turn(turn_id)
        print(f"[audio] sending {len(pcm) / 32_000:.2f}s of approved speech to Qwen STT.",
              flush=True)

        async def send() -> None:
            for offset in range(0, len(pcm), 3200):
                await self.stt.send_audio(pcm[offset:offset + 3200])
            await self.stt.finish_turn()

        sender = asyncio.create_task(send())
        try:
            async with asyncio.timeout(15):
                await sender
                async for result in self.stt.results():
                    if result.turn_id != turn_id or not result.is_final:
                        continue
                    if result.text == "[STT complete]" or result.text.startswith("[STT error]"):
                        return ""
                    return result.text.strip() if self._accept_transcript(result.text) else ""
        except TimeoutError:
            print("[audio] Qwen STT timed out; listening again.", flush=True)
        finally:
            if not sender.done():
                sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            if sender.cancelled() or sender.exception() is not None:
                await self.stt.finish_turn()
        return ""

    async def _listen(self, turn_id: UUID) -> str:
        # Follow-ups and pending approvals already belong to this conversation:
        # stream them straight to Qwen instead of paying for a second decode.
        conversation_open = (
            time.monotonic() < self._active_until
            or self.background.approval_model is not None
            or getattr(self.llm._tools, "has_pending_approval", False) is True
        )
        detector = self.local_wake_stt if not conversation_open else None
        # Keep music out of cloud STT even during the active window, but allow
        # local keyword activation so "ATHENA, pause music" still works.
        if (not listen_while_music() and self._music_is_audible()
                and getattr(self.local_wake_stt, "streaming", False) is True):
            detector = self.local_wake_stt
        keyword_gate = detector if getattr(detector, "streaming", False) is True else None
        if detector is not None and keyword_gate is None:
            return await self._listen_local_wake(turn_id)
        if keyword_gate is not None:
            keyword_gate.reset()
        self.state = AgentState.LISTENING
        self.settings_store.reload()
        self.voice_gate.configure(
            minimum_rms=self.settings_store.get("vad_minimum_rms"),
            noise_multiplier=self.settings_store.get("vad_noise_multiplier"),
            end_silence_ms=self.settings_store.get("vad_end_silence_ms"),
            start_ms=self.settings_store.get("vad_start_ms"),
            minimum_speech_ms=self.settings_store.get("vad_minimum_speech_ms"),
        )
        self.voice_gate.reset()
        self.audio_status.state(LISTENING, turn=str(turn_id))
        self.audio_status.heard("")
        # Do not open a cloud STT turn while the room is silent. The local VAD
        # runs first and only starts DashScope after speech is confirmed; this
        # saves idle sessions, audio transfer, and the connection setup delay.
        audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        speech_started = asyncio.Event()
        capture_finished = asyncio.Event()

        def music_is_playing() -> bool:
            """Cheap local check; a status dict, never any I/O.

            The dict type-check keeps test doubles honest: a Mock's status()
            would otherwise look like "playing" and silence every turn.
            """
            try:
                tool = self.llm._tools.get("netease_music")
            except AttributeError:
                return False
            status = getattr(getattr(tool, "player", None), "status", None)
            if not callable(status):
                return False
            try:
                state = status()
            except Exception:
                return False
            return isinstance(state, dict) and bool(state.get("playing")) and not state.get("paused", False)

        async def capture() -> None:
            frames_seen = 0
            was_active = False
            streamed_bytes = 0
            retained_frames = 0
            cap = segment_cap_bytes()
            music_note_shown = False
            keyword_buffer = deque(maxlen=150)  # Three seconds of name/onset.

            async def approve_keyword() -> None:
                self._keyword_verified_turn = turn_id
                speech_started.set()
                for packet in keyword_buffer:
                    await audio_queue.put(packet)
                keyword_buffer.clear()

            def retained_seconds() -> float:
                return retained_frames * self.voice_gate.frame_ms / 1000.0

            def dropped() -> tuple[int, float]:
                return (int(getattr(self.microphone, "dropped_frames", 0) or 0),
                        float(getattr(self.microphone, "dropped_seconds", 0.0) or 0.0))

            async for frame in self.microphone.frames():
                if self.active_turn != turn_id:
                    capture_finished.set()
                    return
                accepted_frames = self.voice_gate.process(frame)
                if (accepted_frames and keyword_gate is None and not speech_started.is_set()
                        and not listen_while_music() and music_is_playing()):
                    # The "speech" is the song itself. Streaming it bills every
                    # second and the transcript comes back as phantom commands.
                    if not music_note_shown:
                        music_note_shown = True
                        print("[audio] music is playing; not listening until "
                              "it stops (ATHENA_LISTEN_WHILE_MUSIC=1 to change)",
                              flush=True)
                    self.voice_gate.reset()
                    if keyword_gate is not None:
                        keyword_gate.reset()
                    continue
                for accepted_frame in accepted_frames:
                    streamed_bytes += len(accepted_frame)
                    retained_frames += 1
                    if keyword_gate is not None and not speech_started.is_set():
                        keyword_buffer.append(accepted_frame)
                        if await keyword_gate.process(accepted_frame):
                            await approve_keyword()
                    else:
                        speech_started.set()
                        await audio_queue.put(accepted_frame)
                if streamed_bytes >= cap:
                    # A segment that never goes quiet (music, a busy room) is
                    # cut here: what was said so far is finalised and answered
                    # instead of streaming unbounded audio into a billed socket.
                    print(f"[audio] segment cap reached "
                          f"({streamed_bytes // 32_000}s); finalising the transcript.",
                          flush=True)
                    await audio_queue.put(None)
                    capture_finished.set()
                    return
                frames_seen += 1
                # Publish the monitor state twice a second plus on every change.
                # Writing it on every fifth frame cost a synchronous file write
                # ten times a second inside the capture loop for no visible gain.
                if frames_seen % 25 == 0 or self.voice_gate.active != was_active:
                    dropped_frames, dropped_seconds = dropped()
                    self.audio_status.update(
                        rms=self.voice_gate.last_rms,
                        noise=self.voice_gate.noise_rms,
                        threshold=self.voice_gate.threshold,
                        speech=self.voice_gate.active,
                        voiced_frames=self.voice_gate.voiced_frames,
                        retained_frames=retained_frames,
                        retained_seconds=retained_seconds(),
                        dropped_frames=dropped_frames,
                        dropped_seconds=dropped_seconds,
                    )
                if self.audio_debug and (frames_seen % 50 == 0 or self.voice_gate.active != was_active):
                    print(
                        "[audio] "
                        f"rms={self.voice_gate.last_rms:.0f} "
                        f"noise={self.voice_gate.noise_rms:.0f} "
                        f"threshold={self.voice_gate.threshold:.0f} "
                        f"speech={'yes' if self.voice_gate.active else 'no'} "
                        f"voiced_frames={self.voice_gate.voiced_frames} "
                        f"retained={retained_seconds():.2f}s "
                        f"dropped={dropped()[1]:.2f}s",
                        flush=True,
                    )
                was_active = self.voice_gate.active
                if self.voice_gate.should_end:
                    if keyword_gate is not None and not speech_started.is_set():
                        if await keyword_gate.process(b"", final=True):
                            await approve_keyword()
                        else:
                            print("[audio] background speech ignored locally; cloud audio=0s.", flush=True)
                            capture_finished.set()
                            return
                    # Commit locally as soon as the silence window closes. The
                    # sender task will finish the cloud turn after queued PCM.
                    dropped_frames, dropped_seconds = dropped()
                    print(f"[audio] end of speech after {retained_seconds():.2f}s "
                          f"retained ({retained_frames} frames), "
                          f"{dropped_seconds:.2f}s dropped", flush=True)
                    self.audio_status.endpoint(
                        turn=str(turn_id),
                        frames=retained_frames,
                        seconds=retained_seconds(),
                        dropped_frames=dropped_frames,
                        dropped_seconds=dropped_seconds,
                    )
                    await self._play_end_of_speech_tone()
                    await audio_queue.put(None)
                    capture_finished.set()
                    return
            await audio_queue.put(None)
            capture_finished.set()

        capture_task = None
        sender_task = None
        cloud_started = False
        waits: list[asyncio.Task] = []
        try:
            capture_task = asyncio.create_task(capture())
            waits = [asyncio.create_task(speech_started.wait()),
                     asyncio.create_task(capture_finished.wait())]
            started_wait, finished_wait = waits
            done, pending = await asyncio.wait(
                set(waits), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if finished_wait in done and not speech_started.is_set():
                return ""
            if self.active_turn != turn_id or not self.voice_gate.active:
                return ""
            cloud_started = True
            await self.stt.start_turn(turn_id)
            print("\nListening...")

            async def send_queued_audio() -> None:
                while True:
                    packet = await audio_queue.get()
                    if packet is None:
                        await self.stt.finish_turn()
                        return
                    # Alibaba recommends roughly 100 ms per streaming packet.
                    # Coalesce the local 20 ms frames before sending.
                    batch = bytearray(packet)
                    while len(batch) < 3200 and not audio_queue.empty():
                        extra = audio_queue.get_nowait()
                        if extra is None:
                            await self.stt.send_audio(bytes(batch))
                            await self.stt.finish_turn()
                            return
                        batch.extend(extra)
                    await self.stt.send_audio(bytes(batch))

            sender_task = asyncio.create_task(send_queued_audio())
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
                        self.audio_status.heard("")
                        return ""
                    if not self._accept_transcript(result.text):
                        print("Ignored: sound was too short to be speech.")
                        self.audio_status.heard(result.text)
                        self.voice_gate.reset()
                        continue
                    self.audio_status.transcript(result.text)
                    return result.text
                self.audio_status.state(SPEECH, turn=str(turn_id))
                self.audio_status.heard(result.text)
        finally:
            self.audio_status.state(WAITING)
            # Cancellation while suspended in asyncio.wait() skips the cleanup
            # above, so the event-wait tasks are always reaped here. Leaving them
            # pending leaked a task on every interrupted listen.
            for task in waits:
                if not task.done():
                    task.cancel()
            if waits:
                await asyncio.gather(*waits, return_exceptions=True)
            if capture_task is not None:
                capture_task.cancel()
                await asyncio.gather(capture_task, return_exceptions=True)
            if sender_task is not None:
                sender_task.cancel()
                await asyncio.gather(sender_task, return_exceptions=True)
            try:
                if cloud_started:
                    await self.stt.finish_turn()
            except Exception as error:
                # This cleanup runs even when a final transcript has already
                # been accepted above. If finish_turn itself fails, letting
                # the exception out of the finally would REPLACE that return
                # value: the user's words would become "Listening failed".
                print(f"[audio] finish_turn cleanup failed: {error}")
        return ""

    def _accept_transcript(self, text):
        return self.voice_gate.has_enough_speech or (
            (self.background.is_confirmation_reply(text) or self.llm.is_confirmation_reply(text)) and self.voice_gate.active
            and self.voice_gate.voiced_frames >= self.voice_gate.start_frames)

    @staticmethod
    def _repeat_key(text: str) -> str:
        """Compare commands loosely: STT punctuation and casing drift between runs."""
        return " ".join(re.findall(r"[a-z0-9]+", text.casefold().replace("'", "").replace("’", "")))

    def _is_repeat(self, text: str) -> bool:
        """True for an immediate, unacknowledged repeat of the last command."""
        if self._last_command_text is None or self._repeat_acknowledged:
            return False
        if time.monotonic() - self._last_command_at > self.REPEAT_WINDOW_SECONDS:
            return False
        if self._repeat_key(text) != self._repeat_key(self._last_command_text):
            return False
        self._repeat_acknowledged = True
        return True

    def _remember_command(self, text: str) -> None:
        self._last_command_text = text
        self._last_command_at = time.monotonic()
        self._repeat_acknowledged = False

    async def _answer(self, turn_id: UUID, transcript: str) -> None:
        context = self._voice_context()
        await self._answer_stream(
            turn_id, transcript, self.llm.stream_reply(turn_id, transcript, context))

    async def _answer_stream(self, turn_id: UUID, transcript: str, fragments) -> None:
        await self._interruptible_speech(self._answer_stream_impl(turn_id, transcript, fragments))

    async def _interruptible_speech(self, speech):
        recognizer = getattr(self, 'interruption_stt', None)
        if recognizer is None:
            return await speech
        await self._stop_listening()
        from athena.interruptions import hear_interruption
        output = asyncio.create_task(speech)
        monitor = asyncio.create_task(hear_interruption(self.microphone, recognizer,
            minimum_rms=max(400, self.settings_store.get('vad_minimum_rms') or 400)))
        try:
            done, _ = await asyncio.wait((output, monitor), return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                try:
                    interrupted = monitor.result()
                except Exception as error:
                    interrupted = ''
                    print(f'[speech] interruption listener failed ({type(error).__name__})', flush=True)
                if interrupted:
                    output.cancel()
                    await self.cancel_active_turn('user asked to stop speaking')
                    self._active_until = time.monotonic() + 20
                    return
            await output
        finally:
            output.cancel()
            monitor.cancel()
            await asyncio.gather(output, monitor, return_exceptions=True)

    async def _answer_stream_impl(self, turn_id: UUID, transcript: str, fragments) -> None:
        """Speak model fragments as they arrive from a foreground or queued job."""
        self.state = AgentState.THINKING
        self._reply_timing = {'turn': turn_id, 'started': time.monotonic(), 'first_text': None}
        self._reply_timing['eos_at'] = getattr(self, '_delivery_eos_at', None)
        self._reply_timing['first_text_at'] = getattr(self, '_delivery_first_text_at', None)
        self._delivery_eos_at = self._delivery_first_text_at = None
        chunker = SpeechChunker()
        native_voice = bool(getattr(self.speaker, "can_speak_text", False))
        playback_task = None if native_voice else asyncio.create_task(self._play_audio(turn_id))
        used_pcm = False

        async def emit(clause):
            nonlocal native_voice, used_pcm, playback_task
            from athena.tts.text import speech_text
            clause = speech_text(clause)
            if not clause:
                return
            if native_voice:
                async with self._audio_focus():
                    try:
                        spoken = await self.speaker.speak_text(clause)
                    except Exception:
                        spoken = False
                if spoken:
                    return
                native_voice = False
                print("[speech] browser speech failed; trying synthesized audio.", flush=True)
            used_pcm = True
            if playback_task is None:
                playback_task = asyncio.create_task(self._play_audio(turn_id))
            await self._send_clause(turn_id, clause)
        response_parts: list[str] = []
        sent_audio = False
        spoken_characters = 0
        skipped_characters = 0
        print("A.T.H.E.N.A: ", end="", flush=True)
        try:
            async for fragment in fragments:
                if self.active_turn != turn_id:
                    return
                response_parts.append(fragment)
                print(fragment, end="", flush=True)
                timing = getattr(self, '_reply_timing', {})
                if fragment and timing.get('turn') == turn_id and timing.get('first_text') is None:
                    timing['first_text'] = time.monotonic() - timing['started']
                for clause in chunker.feed(fragment):
                    if self._budget_reached(spoken_characters):
                        skipped_characters += len(clause)
                        continue
                    self.state = AgentState.SPEAKING
                    await emit(clause)
                    spoken_characters += len(clause)
                    sent_audio = True
            for clause in chunker.finish():
                if self.active_turn != turn_id:
                    return
                if self._budget_reached(spoken_characters):
                    skipped_characters += len(clause)
                    continue
                await emit(clause)
                spoken_characters += len(clause)
                sent_audio = True
            print()
            if skipped_characters:
                print(f"[speech stopped by ATHENA_TTS_MAX_CHARS after "
                      f"{spoken_characters} characters; {skipped_characters} not spoken]",
                      flush=True)
            if sent_audio and not used_pcm:
                print("[speech] streamed reply spoken by the connected computer (no cloud TTS).", flush=True)
            if sent_audio and used_pcm:
                # Bound synthesis, which can stall on the network — but NOT
                # playback. Putting the playback inside the same 30 second clock
                # cut the audio off mid-sentence on any reply longer than about
                # half a minute, while the log showed the whole answer. Speaking
                # a long briefing legitimately takes longer than that.
                async with asyncio.timeout(synthesis_timeout()):
                    await self.tts.flush(turn_id)
                # A generous ceiling, so a genuinely wedged speaker still cannot
                # hang the turn and stop the assistant answering.
                async with asyncio.timeout(PLAYBACK_TIMEOUT_SECONDS):
                    played_bytes = await playback_task
                # Two numbers, so a short answer can be told apart from a dropped
                # one: bytes on the wire against characters sent to synthesis.
                seconds = (played_bytes or 0) / 48_000      # 24 kHz, 16-bit mono
                charge = (f"CNY {spoken_characters / 10_000:.4f}"
                          if speech_is_billed(self.tts)
                          else speech_cost_label(self.tts))
                print(f"[speech] {spoken_characters} characters "
                      f"({charge}), "
                      f"{played_bytes or 0} bytes played (~{seconds:.1f}s)",
                      flush=True)
            assistant_text = "".join(response_parts).strip()
            if assistant_text:
                await self.memory.remember_turn(turn_id, transcript, assistant_text)
        except TimeoutError:
            print("\nSpeech output timed out; the answer is printed above.")
        except DeepSeekUnavailable as error:
            # One failed turn must not end an always-on service.
            print(f"\n{error}")
        except Exception as error:
            # Speech synthesis or memory can fail too. Report it and keep the
            # assistant running instead of letting the turn kill the service.
            print(f"\nThat turn failed ({type(error).__name__}); the answer is printed above.")
        finally:
            if playback_task is not None and not playback_task.done():
                playback_task.cancel()
            # Always retrieve the playback task: if it already finished with an
            # exception, not awaiting it hides the failure and warns at shutdown.
            if playback_task is not None:
                await asyncio.gather(playback_task, return_exceptions=True)
            # Release the synthesis session. Without this a superseded answer
            # left its realtime socket open and billing until some later turn
            # happened to clean it up — or never, if the user walked away.
            await self.tts.cancel(turn_id)
            self.state = AgentState.IDLE
            if self.wake_word and self._active_until > 0 and self.active_turn == turn_id:
                self._active_until = time.monotonic() + 20.0

    def _cached_speech(self, text: str) -> bytes | None:
        """Audio for text that has already been spoken, if it is still held."""
        return self._speech_cache.get(text)

    def _remember_speech(self, text: str, pcm: bytes) -> None:
        """Keep recently spoken audio so the same words are never paid for twice.

        A watch, a status line, a repeated refusal — the same sentence comes back
        around, and synthesis is the expensive part of a reply. Only exact repeats
        are served from here, so nothing is ever spoken with the wrong words.
        """
        if not pcm or len(text) < 8:
            return
        self._speech_cache[text] = pcm
        while len(self._speech_cache) > SPEECH_CACHE_ENTRIES:
            self._speech_cache.popitem(last=False)

    async def _play_audio(self, turn_id: UUID, collect: list | None = None) -> int:
        """Play a turn's audio and report how many bytes reached the speaker.

        When `collect` is given, the audio is also appended to it so the caller
        can keep the phrase for a free replay next time.
        """
        played = 0
        async with AsyncExitStack() as focus:
            focused = False
            async for chunk in self.tts.audio(turn_id):
                if chunk.turn_id != turn_id:
                    continue
                if self.active_turn != turn_id:
                    print(f"[speech] playback stopped: turn {turn_id} was superseded "
                          f"after {played} bytes", flush=True)
                    return played
                if not focused:
                    await focus.enter_async_context(self._audio_focus())
                    focused = True
                    timing = getattr(self, '_reply_timing', {})
                    if timing.get('turn') == turn_id:
                        from athena.metrics import record
                        sample = {
                            'answer_to_audio_ready_ms': round((time.monotonic() - timing['started']) * 1000),
                            'answer_to_first_text_ms': round((timing.get('first_text') or 0) * 1000),
                            'note': 'Endpoint includes silence detection; audio ready is not physical speaker latency.'}
                        eos = timing.get('eos_at')
                        if eos and 0 <= time.time() - eos < 600:
                            sample['endpoint_to_audio_ready_ms'] = round((time.time() - eos) * 1000)
                            if timing.get('first_text_at'):
                                sample['endpoint_to_first_text_ms'] = round(max(0, timing['first_text_at'] - eos) * 1000)
                        asyncio.create_task(asyncio.to_thread(record, 'voice_latency', sample))
                await self.speaker.play(chunk.pcm)
                played += len(chunk.pcm)
                if collect is not None:
                    collect.append(chunk.pcm)
        return played

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
        # A warm-up still running would otherwise be left holding a Piper
        # process past shutdown.
        if self._warm_task is not None and not self._warm_task.done():
            self._warm_task.cancel()
            await asyncio.gather(self._warm_task, return_exceptions=True)
        self._warm_task = None
        # Both background players own a child process and the speaker stream, so
        # either one left running would keep making noise after shutdown.
        for name in ("netease_music", "youtube_audio"):
            player = getattr(self.llm._tools.get(name), "player", None)
            if player is not None:
                await player.stop()
        await self.cancel_active_turn("shutdown")
        await asyncio.gather(
            self.stt.close(),
            *([self.interruption_stt.close()] if self.interruption_stt is not None else []),
            *( [self.local_wake_stt.close()] if self.local_wake_stt is not None else []),
            self.tts.close(),
            self.llm.close(),
            self.memory.close(),
            self.microphone.close(),
            self.speaker.close(),
            return_exceptions=True,
        )
