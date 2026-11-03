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
        # The board's own speaker, opened only if music ever needs it, and the
        # one-time verdict on whether it can be opened at all.
        self._fallback = None
        self._fallback_failed = False
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

    async def _pi_output(self):
        """The board's own speaker, opened on first use and kept open.

        ATHENA is told to route audio to the computer, but that computer's
        browser can be closed, asleep or simply never opened. Music that hard
        fails then is the one thing the user always notices, so a browser that
        is not attached falls back to the Pi instead of refusing to play.
        """
        if self._fallback_failed:
            # Already refused once in this run. Retrying would only add latency
            # to every chunk, and returning None here would drop the audio
            # silently, so the reason is repeated instead.
            raise RuntimeError(
                "The computer browser is not connected, and the Pi's own speaker "
                "has already failed to open.")
        if self._fallback is None:
            try:
                _, speaker = self.factories["pi"]()
                await speaker.open()
            except BaseException as error:
                self._fallback_failed = True
                raise RuntimeError(
                    "The computer browser is not connected and the Pi's own speaker "
                    f"could not be opened either ({error}).") from None
            self._fallback = speaker
        return self._fallback

    async def _target_for_music(self):
        """Which speaker music should use right now.

        The live output when the computer is genuinely attached, the Pi's own
        speaker when it is not. Raises rather than returning something that
        cannot make a sound, so a track never reports success while playing
        nothing.
        """
        if self.target != "computer" or self.audio.attached:
            return self.output
        return await self._pi_output()

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
            if self._fallback is not None:
                await asyncio.gather(self._fallback.close(), return_exceptions=True)
                self._fallback = None

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
    def available(self):
        """Whether music can make a sound right now.

        A routed computer that no browser has attached still has the Pi's own
        speaker behind it, so this must not simply forward the browser's own
        `available` — that reports False and made every track refuse to start
        even though the board could have played it.
        """
        if getattr(self.router.output, "available", True):
            return True
        return not self.router._fallback_failed
    @property
    def music_format(self):
        """The format music should be decoded to for whatever will play it.

        A browser takes full-rate stereo while the Pi's own stream is mono, so
        this has to follow whichever speaker is actually live — otherwise the
        decoder and the output disagree and the track comes out at the wrong
        pitch.
        """
        if self.router.target == "computer" and not self.router.audio.attached:
            fallback = self.router._fallback
            if fallback is not None:
                return getattr(fallback, "music_format", (24_000, 1))
            return (24_000, 1)
        return getattr(self.router.output, "music_format", (24_000, 1))
    async def open(self): await self.router.open()
    async def close(self): await self.router.close()
    async def play(self, pcm, rate=None, channels=None):
        async with self.router.lock:
            if self.router.target == "computer":
                if self.router.audio.attached:
                    await self.router.output.play(pcm, rate=rate, channels=channels)
                    return
                # The browser is not attached. Hand the track to the board's own
                # speaker rather than dropping it, and let a genuine failure
                # raise so the tool reports it instead of claiming success.
                await self._play_local(await self.router._target_for_music(), pcm, rate, channels)
                return
            await self._play_local(self.router.output, pcm, rate, channels)
    async def _play_local(self, output, pcm, rate=None, channels=None):
        # A track already decoded at the computer's stereo rate keeps playing
        # correctly after handoff to the Pi's mono stream.
        target_rate = getattr(output, "_sample_rate", rate)
        if rate and channels and (channels != 1 or rate != target_rate):
            samples = array("h"); samples.frombytes(pcm)
            if sys.byteorder != "little": samples.byteswap()
            frames = len(samples) // channels
            converted = array("h", (int(sum(samples[int(i * rate / target_rate) * channels:int(i * rate / target_rate) * channels + channels]) / channels)
                for i in range(int(frames * target_rate / rate))))
            if sys.byteorder != "little": converted.byteswap()
            pcm = converted.tobytes()
        await output.play(pcm)
    async def stop(self):
        await self.router.output.stop()
        fallback = self.router._fallback
        if fallback is not None:
            try:
                await fallback.stop()
            except Exception:
                pass
    async def speak_text(self, text): return await self.router.output.speak_text(text)
    async def finish_playback(self):
        finish = getattr(self.router.output, "finish_playback", None)
        if finish is not None: await finish()
        fallback = self.router._fallback
        finish = getattr(fallback, "finish_playback", None) if fallback is not None else None
        if finish is not None: await finish()
