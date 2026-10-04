"""Atomic microphone/speaker handoff; music keeps the same speaker facade."""
import asyncio
from array import array
import sys

class AudioRouter:
    def __init__(self, audio, pi_factory, computer_factory, target="pi"):
        self.audio, self.factories = audio, {"pi": pi_factory, "computer": computer_factory}
        self.target = target
        self.audio.selected_route = target
        self.input, self.output = self.factories[target]()
        self.lock = asyncio.Lock()
        self.opened = False
        self.microphone = RoutedMicrophone(self)
        self.speaker = RoutedSpeaker(self)

    def status(self):
        return {"target": self.target, "computer_ready": self.audio.attached,
                "input": "computer default microphone" if self.target == "computer" else "Pi configured microphone",
                "output": "computer default speaker" if self.target == "computer" else "Pi configured speaker"}

    async def open(self):
        async with self.lock:
            if self.opened: return
            try:
                await self.output.open(); await self.input.open()
                self.opened = True
            except BaseException:
                await asyncio.gather(self.input.close(), self.output.close(), return_exceptions=True)
                raise

    async def switch(self, target):
        if target not in self.factories: raise ValueError("Choose pi or computer.")
        async with self.lock:
            if target == self.target: return self.status()
            if target == "computer" and not self.audio.attached:
                raise RuntimeError("Open the HTTPS control page on your computer and click Microphone and speaker Start once. Your current devices are unchanged.")
            microphone, speaker = self.factories[target]()
            try:
                await speaker.open(); await microphone.open()
            except BaseException:
                await asyncio.gather(microphone.close(), speaker.close(), return_exceptions=True)
                raise RuntimeError("The destination microphone or speaker could not open. Your current devices are unchanged.") from None
            old_input, old_output = self.input, self.output
            try:
                await old_output.stop()
            except BaseException:
                await asyncio.gather(microphone.close(), speaker.close(), return_exceptions=True)
                raise
            level = getattr(old_output, "volume", None)
            if isinstance(level, int) and hasattr(speaker, "set_volume"): speaker.set_volume(level)
            self.input, self.output, self.target = microphone, speaker, target
            self.audio.selected_route = target
            self.audio.drain()
            await asyncio.gather(old_input.close(), old_output.close(), return_exceptions=True)
            # This is a UI notification, not part of the atomic hardware commit.
            try: await self.audio.send_json({"type": "audio_route", "target": target})
            except Exception: pass
            return self.status()

    async def close(self):
        async with self.lock:
            if self.opened:
                await asyncio.gather(self.input.close(), self.output.close(), return_exceptions=True)
                self.opened = False

class RoutedMicrophone:
    def __init__(self, router): self.router = router
    def __getattr__(self, name): return getattr(self.router.input, name)
    async def open(self): await self.router.open()
    async def close(self): await self.router.close()
    async def frames(self):
        async for frame in self.router.input.frames(): yield frame

class RoutedSpeaker:
    def __init__(self, router): self.router = router
    def __getattr__(self, name): return getattr(self.router.output, name)
    @property
    def can_speak_text(self): return bool(getattr(self.router.output, "can_speak_text", False))
    @property
    def available(self): return getattr(self.router.output, "available", True)
    async def open(self): await self.router.open()
    async def close(self): await self.router.close()
    async def play(self, pcm, rate=None, channels=None):
        async with self.router.lock:
            if self.router.target == "computer":
                await self.router.output.play(pcm, rate=rate, channels=channels)
            else:
                # A track already decoded at the computer's stereo rate keeps
                # playing correctly after handoff to the Pi's mono stream.
                target_rate = getattr(self.router.output, "_sample_rate", rate)
                if rate and channels and (channels != 1 or rate != target_rate):
                    samples = array("h"); samples.frombytes(pcm)
                    if sys.byteorder != "little": samples.byteswap()
                    frames = len(samples) // channels
                    converted = array("h", (int(sum(samples[int(i * rate / target_rate) * channels:int(i * rate / target_rate) * channels + channels]) / channels)
                        for i in range(int(frames * target_rate / rate))))
                    if sys.byteorder != "little": converted.byteswap()
                    pcm = converted.tobytes()
                await self.router.output.play(pcm)
    async def stop(self): await self.router.output.stop()
    async def speak_text(self, text): return await self.router.output.speak_text(text)
    async def finish_playback(self):
        finish = getattr(self.router.output, "finish_playback", None)
        if finish is not None: await finish()
