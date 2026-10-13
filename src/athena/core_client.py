"""Thin voice client for the local OpenClaw core."""
import asyncio
import json
import os
import time
from uuid import uuid4
import aiohttp
from athena.core import control_config
from athena.tools.registry import ToolRegistry


class OperationalMemory:
    """OpenClaw owns sessions; do not duplicate or consolidate personal history."""
    async def connect(self): pass
    async def close(self): pass
    async def remember_turn(self, *args, **kwargs): pass
    def context_messages(self, *args, **kwargs): return []


class CoreLanguageModel:
    gateway_backed = True
    shutdown_requested = False

    def __init__(self, session='voice'):
        self.session = session
        self._tools = ToolRegistry()
        self.http = None
        self.requests = {}

    async def connect(self):
        if self.http is None:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240))
        self.config = json.loads(control_config().read_text())

    def fork(self):
        return self

    def is_confirmation_reply(self, text):
        return text.strip().lower() in {'yes', 'no', 'approve', 'deny'}

    async def stream_reply(self, turn_id, text, context=None, on_connected=None):
        await self.connect()
        headers = {'Authorization': 'Bearer ' + self.config['token']}
        async with self.http.post(self.config['url'] + '/v1/chat', headers=headers,
                json={'text': text, 'session': self.session, 'mode':'voice', 'request_id': str(turn_id)}) as response:
            if response.status != 200:
                raise RuntimeError('ATHENA core is unavailable.')
            self.requests[turn_id] = response
            try:
                if on_connected: on_connected()
                budget = SpokenBudget()
                async for raw in response.content:
                    event = json.loads(raw)
                    if event['type'] == 'text':
                        spoken = budget.feed(event['text'])
                        if spoken: yield spoken
                    elif event['type'] == 'replace':
                        spoken = budget.feed('Correction. ' + event['text'])
                        if spoken: yield spoken
                    elif event['type'] == 'error':
                        yield 'I could not finish that request. The console has the error.'
            finally:
                self.requests.pop(turn_id, None)

    async def cancel(self, turn_id):
        # Disconnect playback delivery only; background tasks are independent.
        if response := self.requests.get(turn_id):
            response.close()

    async def close(self):
        if self.http:
            await self.http.close()


class SpokenBudget:
    """Bound speech without buffering the full response or cancelling workers."""
    def __init__(self, limit=45):
        self.limit = limit
        self.words = 0
        self.in_word = False
        self.done = False

    def feed(self, text):
        if self.done: return ''
        output = []
        for char in text:
            if not char.isspace() and not self.in_word:
                if self.words >= self.limit:
                    self.done = True
                    return ''.join(output).rstrip() + '. The details are in the console.'
                self.words += 1
            self.in_word = not char.isspace()
            output.append(char)
        return ''.join(output)


def spoken_update(event):
    if event['type']=='approval': return 'A task needs approval. The action and approval button are in the console.'
    state = event['data'].get('state')
    return {'verified':'Your task is finished and its checks passed.',
        'reply_received':'Your task has a result. The report is in the console.',
        'failed':'Your task failed. The console has the error.',
        'cancelled':'Your task was cancelled.',
        'unconfirmed':'I lost confirmation of a task. Check its status in the console.',
        'verifying':'The worker finished. I am checking the result.',
        'denied':'The task was denied.'}.get(state,'There is a task update in the console.')


async def notifications(coordinator):
    config = json.loads(control_config().read_text())
    cursor_path = control_config().parent / 'voice-event-cursor.json'
    try: cursor = json.loads(cursor_path.read_text())['seq']
    except (OSError, ValueError): cursor = 0
    started = time.time()
    async with aiohttp.ClientSession() as http:
        while True:
            try:
                async with http.get(config['url'] + '/v1/events', params={'after': cursor},
                        headers={'Authorization': 'Bearer ' + config['token']}) as response:
                    if response.status != 200:
                        config = json.loads(control_config().read_text())
                        await asyncio.sleep(2)
                        continue
                    body = await response.json()
                for event in body['events']:
                    # Past operational records remain in the console. Starting
                    # voice must not recite the entire historical test backlog.
                    if event['time'] < started:
                        cursor = event['seq']
                        cursor_path.write_text(json.dumps({'seq':cursor}))
                        continue
                    if event['type'] in {'terminal', 'approval', 'milestone'}:
                        queue = getattr(coordinator,'_external_speech',None)
                        if queue is not None and queue.full(): break
                        text = spoken_update(event)
                        if not coordinator.enqueue_external_speech(text):
                            break
                    cursor = event['seq']
                    cursor_path.write_text(json.dumps({'seq': cursor}))
            except (aiohttp.ClientError, OSError, ValueError):
                pass
            await asyncio.sleep(1)


async def timed_fragments(fragments, delay=.2, minimum=24):
    """Keep the next token read alive while offering bounded speech flush ticks."""
    iterator = fragments.__aiter__()
    pending = asyncio.create_task(anext(iterator))
    size = 0
    deadline = None
    try:
        while True:
            wait = max(0, deadline - time.monotonic()) if size >= minimum and deadline else None
            done, _ = await asyncio.wait({pending}, timeout=wait)
            if not done:
                size = 0; deadline = None
                yield None
                continue
            try: fragment = pending.result()
            except StopAsyncIteration: return
            pending = asyncio.create_task(anext(iterator))
            size += len(fragment)
            if deadline is None: deadline = time.monotonic() + delay
            yield fragment
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def run_voice():
    from athena.audio.capture import Microphone
    from athena.audio.playback import Speaker
    from athena.audio.vad import VoiceGate
    from athena.coordinator import VoiceCoordinator
    from athena.config import Settings, load_local_environment
    from athena.settings.store import RuntimeSettingsStore
    from athena.stt import build_local_wake_recognizer, build_recognizer
    from athena.tts import build_synthesizer, synthesizer_sample_rate
    load_local_environment()
    try:
        profile=json.loads((control_config().parent/'voice-profile.json').read_text())
        os.environ.update({k:str(v) for k,v in profile.items() if k in
            {'ATHENA_TTS_BACKEND','ATHENA_WINDOWS_TTS_BACKEND','ATHENA_EDGE_VOICE','ATHENA_EDGE_TRANSPORT','ATHENA_EDGE_RATE'}})
    except (OSError,ValueError):pass
    store = RuntimeSettingsStore()
    settings = Settings.from_environment(store)
    coordinator = VoiceCoordinator(
        microphone=Microphone(settings.stt_sample_rate, device=settings.audio_input_device),
        speaker=Speaker(synthesizer_sample_rate(settings), device=settings.audio_output_device),
        stt=build_recognizer(settings), interruption_stt=build_recognizer(settings),
        local_wake_stt=build_local_wake_recognizer(settings), llm=CoreLanguageModel(),
        tts=build_synthesizer(settings, store), memory=OperationalMemory(),
        voice_gate=VoiceGate(minimum_rms=settings.vad_minimum_rms, noise_multiplier=settings.vad_noise_multiplier,
                             end_silence_ms=settings.vad_end_silence_ms), settings_store=store)
    listener = None
    health = control_config().parent / 'voice-health.json'
    def publish(state, **fields):
        health.write_text(json.dumps({'state':state,'pid':os.getpid(),
            'instance':os.environ.get('ATHENA_VOICE_INSTANCE'),**fields}))
    publish('starting')
    try:
        await coordinator.connect()
        publish('listening', tts=type(coordinator.tts).__name__,
            voice=getattr(coordinator.tts,'voice_name',getattr(coordinator.tts,'_voice',None)))
        listener = asyncio.create_task(notifications(coordinator))
        await coordinator.run()
    except Exception as error:
        publish('failed', error=type(error).__name__)
        raise
    finally:
        if listener:
            listener.cancel(); await asyncio.gather(listener, return_exceptions=True)
        await coordinator.close()
