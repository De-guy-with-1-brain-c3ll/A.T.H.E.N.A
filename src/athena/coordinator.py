from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime
import os
import random
import re
import time
from uuid import UUID, uuid4

from athena.audio.capture import Microphone
from athena.audio.playback import Speaker
from athena.audio.vad import VoiceGate
from athena.audio.telemetry import AudioStatusWriter
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


class VoiceCoordinator:
    WAKE_ACKS = ("Yes?", "What's up?", "I'm listening.", "Go ahead.",
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
        self._alarm_pcm = b""
        self._warm_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._external_speech: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
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
        await asyncio.gather(
            self.microphone.open(),
            self.speaker.open(),
            self.stt.connect(),
            self.llm.connect(),
            self.tts.connect(),
            self.memory.connect(),
        )
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
                        if text.casefold().startswith("alarm:"):
                            # An alarm the user set always rings, whatever the hour.
                            await self._speak_text(text)
                            await self.speaker.play(self._alarm_pcm)
                        elif self.quiet_hours():
                            # Nothing unprompted is spoken overnight. It is not
                            # lost: it is printed here and shown on the dashboard.
                            print(f"[quiet hours, not spoken] {text}", flush=True)
                        else:
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
                        transcript = self._wake_command(transcript, allow_confirmation=is_confirmation)
                        if transcript is None:
                            print("Ignored: wake word not detected.", flush=True)
                            continue
                        self._active_until = time.monotonic() + 20.0
                        await self.speaker.play(self._ack_pcm)
                        if self._sleeping:
                            # Saying the wake word is how sleep mode is left.
                            await self._wake_from_sleep()
                            if not transcript:
                                continue
                        if not transcript and await self._offer_brief():
                            continue
                        if not transcript:
                            await self._speak_text(random.choice(self.WAKE_ACKS))
                            continue
                if ToolRegistry.is_shutdown_command(transcript):
                    await self.background.cancel_all()
                    await self._answer(self.active_turn, transcript)
                    break
                if self._brief_offer is not None:
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
                    if self._sleeping:
                        awaiting = status_report().replace("\n", " ")
                        pending += f" Also {awaiting[0].lower()}{awaiting[1:]}"
                    await self._speak_text(pending)
                elif ToolRegistry.is_download_status_query(transcript):
                    await self._speak_text(self.background.download_status().spoken_text)
                elif self.is_memory_status_query(transcript):
                    # Answered from the shared record, so it stays correct when the
                    # pass was started by the dashboard or a scheduled job rather
                    # than by this interface.
                    await self._speak_text(status_report())
                elif self.is_sleep_command(transcript):
                    await self._enter_sleep()
                elif self._is_repeat(transcript):
                    await self._speak_text(random.choice(self.REPEAT_ACKS))
                else:
                    job = self.background.submit(transcript, self._voice_context())
                    if job is None:
                        await self._speak_text("I already have three requests pending. Let one finish, or say cancel background tasks.")
                    else:
                        self._remember_command(transcript)
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
        if self.alerts is None or self._brief_offer is not None:
            return False
        try:
            briefs = self.alerts.brief_rows()
        except Exception:
            return False
        if not briefs:
            return False
        self._brief_offer = briefs[0]["key"]
        if self.quiet_hours():
            print(f"[quiet hours, not spoken] a brief is ready: {briefs[0]['label']}",
                  flush=True)
            return False
        await self._speak_text(f"I've got {briefs[0]['label']}. Want it?")
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
                if job.reply_started:
                    # The model is still streaming.  Start speech now rather
                    # than waiting for the last token to arrive; this is the
                    # critical path for a natural voice response.
                    job.delivery_started = True
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
        finally:
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    async def _speak_text(self, text, prompted: bool = False):
        """Speak text. `prompted` means the user asked for this reply.

        Only a reply they asked for reopens the hands-free window. A watcher
        announcement used to re-arm it too, so for twenty seconds afterwards any
        conversation in the room was taken as a command and answered.
        """
        turn = uuid4()
        self.active_turn = turn
        self.state = AgentState.SPEAKING
        music_tool = self.llm._tools.get("netease_music")
        music_player = getattr(music_tool, "player", None)
        if music_player is not None:
            music_player.suspend_for_voice()
        print(f"A.T.H.E.N.A: {text}")
        cached = self._cached_speech(text)
        if cached:
            # Already spoken before, so it costs nothing to say again.
            print(f"[speech] replayed from cache ({len(cached)} bytes, no synthesis)",
                  flush=True)
            try:
                await self.speaker.play(cached)
            finally:
                self.state = AgentState.IDLE
                if music_player is not None:
                    music_player.resume_after_voice()
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
            if music_player is not None:
                music_player.resume_after_voice()
            if prompted and self.wake_word and self._active_until > 0:
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
            start_ms=self.settings_store.get("vad_start_ms"),
            minimum_speech_ms=self.settings_store.get("vad_minimum_speech_ms"),
        )
        self.voice_gate.reset()
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
            return isinstance(state, dict) and bool(state.get("playing"))

        async def capture() -> None:
            frames_seen = 0
            was_active = False
            streamed_bytes = 0
            cap = segment_cap_bytes()
            music_note_shown = False
            async for frame in self.microphone.frames():
                if self.active_turn != turn_id:
                    capture_finished.set()
                    return
                accepted_frames = self.voice_gate.process(frame)
                if (accepted_frames and not speech_started.is_set()
                        and not listen_while_music() and music_is_playing()):
                    # The "speech" is the song itself. Streaming it bills every
                    # second and the transcript comes back as phantom commands.
                    if not music_note_shown:
                        music_note_shown = True
                        print("[audio] music is playing; not listening until "
                              "it stops (ATHENA_LISTEN_WHILE_MUSIC=1 to change)",
                              flush=True)
                    self.voice_gate.reset()
                    continue
                for accepted_frame in accepted_frames:
                    speech_started.set()
                    await audio_queue.put(accepted_frame)
                    streamed_bytes += len(accepted_frame)
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
                    # Commit locally as soon as the silence window closes. The
                    # sender task will finish the cloud turn after queued PCM.
                    await audio_queue.put(None)
                    capture_finished.set()
                    return
            capture_finished.set()

        capture_task = None
        sender_task = None
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
                        return ""
                    if not self._accept_transcript(result.text):
                        print("Ignored: sound was too short to be speech.")
                        self.voice_gate.reset()
                        continue
                    return result.text
        finally:
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
        """Speak model fragments as they arrive from a foreground or queued job."""
        self.state = AgentState.THINKING
        chunker = SpeechChunker()
        playback_task = asyncio.create_task(self._play_audio(turn_id))
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
                for clause in chunker.feed(fragment):
                    if self._budget_reached(spoken_characters):
                        skipped_characters += len(clause)
                        continue
                    self.state = AgentState.SPEAKING
                    await self._send_clause(turn_id, clause)
                    spoken_characters += len(clause)
                    sent_audio = True
            for clause in chunker.finish():
                if self.active_turn != turn_id:
                    return
                if self._budget_reached(spoken_characters):
                    skipped_characters += len(clause)
                    continue
                await self._send_clause(turn_id, clause)
                spoken_characters += len(clause)
                sent_audio = True
            print()
            if skipped_characters:
                print(f"[speech stopped by ATHENA_TTS_MAX_CHARS after "
                      f"{spoken_characters} characters; {skipped_characters} not spoken]",
                      flush=True)
            if sent_audio:
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
            if not playback_task.done():
                playback_task.cancel()
            # Always retrieve the playback task: if it already finished with an
            # exception, not awaiting it hides the failure and warns at shutdown.
            await asyncio.gather(playback_task, return_exceptions=True)
            # Release the synthesis session. Without this a superseded answer
            # left its realtime socket open and billing until some later turn
            # happened to clean it up — or never, if the user walked away.
            await self.tts.cancel(turn_id)
            self.state = AgentState.IDLE

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
        async for chunk in self.tts.audio(turn_id):
            if chunk.turn_id != turn_id:
                continue
            if self.active_turn != turn_id:
                # A newer turn took over, so this audio is deliberately dropped.
                print(f"[speech] playback stopped: turn {turn_id} was superseded "
                      f"after {played} bytes", flush=True)
                return played
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
