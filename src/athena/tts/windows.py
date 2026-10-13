"""Persistent local Windows speech synthesis, independent of Edge networking."""
import asyncio
import base64
import json
import os
import subprocess
from uuid import UUID
from athena.events import AudioChunk


SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$english = $synth.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'en-*' } | Select-Object -First 1
if ($english) { $synth.SelectVoice($english.VoiceInfo.Name) }
$format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(24000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
[Console]::WriteLine((@{ready=$true;voice=$synth.Voice.Name} | ConvertTo-Json -Compress))
while ($null -ne ($line = [Console]::ReadLine())) {
    try {
        $item = $line | ConvertFrom-Json
        $stream = [IO.MemoryStream]::new()
        $synth.SetOutputToAudioStream($stream, $format)
        $synth.Speak([string]$item.text)
        $pcm = [Convert]::ToBase64String($stream.ToArray())
        $synth.SetOutputToNull()
        $stream.Dispose()
        [Console]::WriteLine((@{pcm=$pcm} | ConvertTo-Json -Compress))
    } catch {
        [Console]::WriteLine((@{error='Windows speech synthesis failed.'} | ConvertTo-Json -Compress))
    }
}
$synth.Dispose()
'''


class WindowsSynthesizer:
    sample_rate = 24000

    def __init__(self):
        self.process = None
        self.task = None
        self.turn = None
        self.pending = asyncio.Queue()
        self.output = asyncio.Queue()
        self.voice_name = None
        self.last_error = None

    async def connect(self):
        if self.process and self.process.returncode is None: return
        if os.name != 'nt': raise RuntimeError('Windows speech requires Windows.')
        encoded = base64.b64encode(SCRIPT.encode('utf-16-le')).decode()
        self.process = await asyncio.create_subprocess_exec('powershell.exe', '-NoProfile', '-NonInteractive',
            '-EncodedCommand', encoded, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=4_000_000, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            result = json.loads(await asyncio.wait_for(self.process.stdout.readline(),15))
            if not result.get('ready'): raise RuntimeError('Windows speech did not become ready.')
            self.voice_name = result['voice']
        except BaseException:
            await self._stop_process()
            raise

    async def send_text(self, turn_id, text):
        if not text.strip(): return
        if self.turn and self.turn != turn_id: await self.cancel(self.turn)
        self.turn = turn_id
        if not self.task or self.task.done():
            self.pending = asyncio.Queue()
            self.task = asyncio.create_task(self._pump(turn_id))
        self.pending.put_nowait(text)

    async def _pump(self, turn_id):
        try:
            await self.connect()
            while (text := await self.pending.get()) is not None:
                self.process.stdin.write((json.dumps({'text':text})+'\n').encode())
                await self.process.stdin.drain()
                result = json.loads(await asyncio.wait_for(self.process.stdout.readline(),30))
                if result.get('error'): raise RuntimeError(result['error'])
                pcm = base64.b64decode(result['pcm'])
                if not pcm: raise RuntimeError('Windows speech produced no audio.')
                for offset in range(0,len(pcm),4096):
                    if self.turn != turn_id: return
                    await self.output.put(AudioChunk(turn_id,pcm[offset:offset+4096]))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = type(error).__name__
            print('[speech] Local Windows speech failed: '+self.last_error,flush=True)
            await self._stop_process()
        finally:
            await self.output.put(turn_id)

    async def audio(self, turn_id=None):
        wanted = turn_id or self.turn
        while True:
            item = await self.output.get()
            if isinstance(item,UUID):
                if item == wanted: return
            elif item.turn_id == wanted: yield item

    async def flush(self, turn_id):
        if self.turn == turn_id and self.task and not self.task.done(): self.pending.put_nowait(None)
        else: self.output.put_nowait(turn_id)

    async def _stop_process(self):
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        self.process = None

    async def cancel(self, turn_id):
        if self.turn != turn_id: return
        self.turn = None
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
            self.task = None
        await self._stop_process()
        self.output.put_nowait(turn_id)

    async def close(self):
        if self.turn: await self.cancel(self.turn)
        await self._stop_process()
