"""Recognize deliberate interruptions without treating speaker echo as a turn."""
import asyncio
import os
import re
from uuid import uuid4
from athena.audio.vad import VoiceGate


def stop_command(text):
    text = re.sub(r'[^a-z ]', ' ', text.casefold())
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^(?:hey )?(?:athena|a thena|a tina|athina)\s+', '', text)
    text = re.sub(r'^please\s+|\s+please$', '', text).strip()
    text = re.sub(r'^(?:ok|okay|now)\s+', '', text).strip()
    text = re.sub(r'\s+(?:now|please|athena)$', '', text).strip()
    # The recogniser returns the infinitive when it is still guessing and the
    # past tense once it has settled, so "stop talking" very often arrives as
    # "stopped talking". Both are the same request, and the live board does
    # exactly this, so the inflection is normalised rather than fought with.
    text = re.sub(r'\bstopped\b', 'stop', text)
    text = re.sub(r'\b(?:talking|speaking|reading|saying)\b', 'talk', text)
    text = re.sub(r'\btalks\b', 'talk', text)
    return bool(re.fullmatch(r'(?:stop(?: (?:talk|it|that))?|'
        r'be quiet|quiet|shut up|enough|that s enough|never ?mind|nevermind|'
        r'cancel|hold on|wait|silence)', text))


async def hear_interruption(microphone, recognizer, minimum_rms=400):
    """Stream bounded segments to a separate recognizer during playback.

    A stop phrase interrupts on a partial. A bare stop word is ambiguous while
    the user is still talking — "stop the timer" starts the same way — so it
    only counts once the segment has closed. Ordinary transcripts, including
    loudspeaker echo, are discarded.
    """
    while True:
        gate = VoiceGate(minimum_rms=minimum_rms, start_ms=80,
            minimum_speech_ms=100, end_silence_ms=260, pre_roll_ms=160)
        turn = uuid4()
        started = asyncio.Event()
        closed = asyncio.Event()
        async def send():
            batch = bytearray()
            count = 0
            try:
                async for frame in microphone.frames():
                    accepted = gate.process(frame)
                    if accepted and not started.is_set():
                        await recognizer.start_turn(turn)
                        started.set()
                    for packet in accepted:
                        batch.extend(packet)
                        count += 1
                    if len(batch) >= 3200:
                        await recognizer.send_audio(bytes(batch))
                        batch.clear()
                    if started.is_set() and (gate.should_end or count >= 150):
                        if os.environ.get('ATHENA_INTERRUPT_DEBUG') == '1':
                            print(f'[speech] interruption segment {count} frames', flush=True)
                        if batch:
                            await recognizer.send_audio(bytes(batch))
                        # Set before returning: the task is not `done()` until
                        # its finally has run, and the transcript that matters
                        # is published from inside that finally.
                        closed.set()
                        return
            finally:
                if started.is_set():
                    closed.set()
                    await recognizer.finish_turn()
        sender = asyncio.create_task(send())
        ready = asyncio.create_task(started.wait())
        try:
            done, _ = await asyncio.wait((sender, ready), return_when=asyncio.FIRST_COMPLETED)
            if sender in done and not started.is_set():
                await sender
                return ''
            async with asyncio.timeout(6):
                async for result in recognizer.results():
                    if os.environ.get('ATHENA_INTERRUPT_DEBUG') == '1':
                        print(f'[speech] interruption heard: {result.text} final={result.is_final} current={result.turn_id == turn}', flush=True)
                    if result.turn_id != turn:
                        continue
                    if result.text.startswith('[STT'):
                        break
                    if stop_command(result.text):
                        words = re.findall(r'[a-z]+', result.text.casefold())
                        # While the user is still speaking, a one-word "stop" is
                        # ambiguous: "stop the timer" begins with the same sound.
                        # Once the segment has closed there is nothing more to
                        # wait for, so a bare stop word has to be honoured or a
                        # short "stop" never interrupts anything.
                        if result.is_final or len(words) >= 2 or closed.is_set():
                            return result.text
        except TimeoutError:
            pass
        finally:
            ready.cancel()
            sender.cancel()
            await asyncio.gather(ready, sender, return_exceptions=True)
        await asyncio.sleep(.1)
