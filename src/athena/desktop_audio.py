"""Native microphone/speaker bridge; bounded PCM queues, explicit opt-in capture."""
from array import array
import json
import math
import queue
import threading
import time

RATE = 16000
FRAME_SAMPLES = 320


def levels(pcm):
    samples = array('h'); samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if not samples: return 0, 0
    return math.sqrt(sum(int(x) * x for x in samples) / len(samples)), max(abs(x) for x in samples)


class FrameConverter:
    """Carry resampling position between blocks; never accumulate stale audio."""
    def __init__(self, rate, channels=1):
        self.ratio = rate / RATE; self.channels = channels
        self.samples = []; self.position = 0.; self.pending = []

    def convert(self, pcm):
        values = array('h'); values.frombytes(pcm[:len(pcm) // 2 * 2])
        mono = [sum(values[i:i + self.channels]) / self.channels
                for i in range(0, len(values) - self.channels + 1, self.channels)]
        if self.ratio == 1:
            output = mono
        else:
            self.samples.extend(mono); output = []
            while self.position + 1 < len(self.samples):
                index = int(self.position); fraction = self.position - index
                output.append(self.samples[index] * (1 - fraction) + self.samples[index + 1] * fraction)
                self.position += self.ratio
            consumed = min(int(self.position), len(self.samples))
            self.samples = self.samples[consumed:]; self.position -= consumed
        self.pending.extend(output); frames = []
        while len(self.pending) >= FRAME_SAMPLES:
            packet = array('h', (max(-32768, min(32767, round(x))) for x in self.pending[:FRAME_SAMPLES]))
            del self.pending[:FRAME_SAMPLES]; frames.append(packet.tobytes())
        return frames


def devices():
    import pyaudio
    audio = pyaudio.PyAudio()
    try:
        result = []
        for index in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(index)
            if info['maxInputChannels']:
                host = audio.get_host_api_info_by_index(info['hostApi'])['name']
                result.append((index, f'{info["name"]} ({host})'))
        return result
    finally: audio.terminate()


def open_socket(client):
    """Pin TLS before sending the session cookie in the websocket upgrade."""
    import websocket
    from athena.desktop import PinnedConnection
    connection = PinnedConnection(client.host, client.fingerprint, timeout=5)
    connection.connect()
    sock = connection.sock; connection.sock = None
    try:
        return websocket.create_connection(f'wss://{client.host}:8780/ws/audio', socket=sock,
            cookie=client.cookie, origin=f'https://{client.host}:8780', timeout=1,
            enable_multithread=True, redirect_limit=0)
    except BaseException:
        sock.close(); raise


class NativeAudio:
    def __init__(self, client, device=None, events=None):
        self.client = client; self.device = device; self.events = events or queue.Queue()
        self.stop_event = threading.Event(); self.ready = threading.Event()
        self.input_frames = queue.Queue(maxsize=8); self.output_frames = queue.Queue(maxsize=50)
        self.lock = threading.Lock(); self.level = (0., 0., 0.)
        self.speech_pending = 0; self.speech_tail = 0.; self.generation = 0
        self.route_active = False; self.ws = None; self.thread = None
        self.failure = ''
        self.rate, self.channels = 24000, 1

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread = threading.Thread(target=self._run, name='athena-desktop-audio', daemon=True)
        self.thread.start()

    def notify(self, text):
        self.events.put(('status', text))

    def fail(self, text):
        self.failure = text; self.notify(text)

    def close(self):
        self.stop_event.set(); self.ready.set()
        if self.ws:
            try: self.ws.shutdown()  # Wake receive/send immediately; cleanup sends no audio.
            except Exception: pass

    def _capture(self, pcm, count, info, status):
        import pyaudio
        if self.stop_event.is_set(): return None, pyaudio.paComplete
        rms, peak = levels(pcm); self.level = (rms, peak, time.monotonic())
        packets = self.converter.convert(pcm)
        if self.route_active:
            for packet in packets:
                try: self.input_frames.put_nowait(packet)
                except queue.Full:
                    try: self.input_frames.get_nowait()
                    except queue.Empty: pass
                    try: self.input_frames.put_nowait(packet)
                    except queue.Full: pass
        return None, pyaudio.paContinue

    def _sender(self):
        try:
            while not self.stop_event.is_set():
                try: packet = self.input_frames.get(timeout=.1)
                except queue.Empty: continue
                if self.route_active:
                    self.ws.send_binary(packet)
        except Exception:
            if not self.stop_event.is_set(): self.fail('Audio connection lost. Stop and reconnect the microphone.')
            self.close()

    def flush(self):
        with self.lock:
            self.generation += 1; self.speech_pending = 0; self.speech_tail = time.monotonic() + .1
        for buffer in (self.input_frames, self.output_frames):
            while True:
                try: buffer.get_nowait()
                except queue.Empty: break

    def control(self, message):
        kind = message.get('type')
        if kind == 'error': raise RuntimeError(message.get('message', 'Voice service unavailable.'))
        if kind == 'audio_route':
            self.route_active = message.get('target') == 'computer'
            self.flush()
            self.notify('PC microphone + speaker active.' if self.route_active else 'PC mic monitoring only; ATHENA is using Pi audio.')
        if kind in {'ready', 'audio_format'}:
            rate = int(message.get('rate', message.get('speaker_rate', 24000)))
            channels = int(message.get('channels', message.get('speaker_channels', 1)))
            if (rate, channels) not in {(24000, 1), (48000, 2)}: raise ValueError('Unsupported speaker format.')
            self.rate, self.channels = rate, channels
            if kind == 'ready': self.ready.set()
        if kind in {'flush', 'speak_stop'}: self.flush()
        if kind == 'speak':
            # Keep Edge TTS on the Pi rather than advertising a browser-only voice.
            self.ws.send(json.dumps({'type': 'speak_done', 'ok': False}))

    def _playback(self, audio):
        import pyaudio
        stream = None; shape = None
        try:
            while not self.stop_event.is_set():
                try: pcm, rate, channels, generation = self.output_frames.get(timeout=.1)
                except queue.Empty: continue
                speech = (rate, channels) == (24000, 1)
                try:
                    if generation != self.generation: continue
                    if shape != (rate, channels):
                        if stream: stream.close()
                        stream = audio.open(format=pyaudio.paInt16, channels=channels, rate=rate,
                            output=True, frames_per_buffer=rate // 50)
                        shape = (rate, channels)
                    step = rate // 50 * channels * 2
                    for offset in range(0, len(pcm), step):
                        if self.stop_event.is_set() or generation != self.generation: break
                        stream.write(pcm[offset:offset + step], exception_on_underflow=False)
                finally:
                    with self.lock:
                        if speech and generation == self.generation:
                            self.speech_pending = max(0, self.speech_pending - 1)
                            self.speech_tail = time.monotonic() + .15
        except Exception:
            if not self.stop_event.is_set(): self.fail('PC speaker unavailable. Check the Windows default output device.')
            self.close()
        finally:
            if stream: stream.close()

    def _run(self):
        audio = None; capture = None; workers = []
        try:
            import pyaudio
            import websocket
            audio = pyaudio.PyAudio()
            info = audio.get_default_input_device_info() if self.device is None else audio.get_device_info_by_index(self.device)
            selected = int(info['index']); found = None
            for rate in dict.fromkeys((16000, int(info['defaultSampleRate']), 48000, 44100)):
                for channels in dict.fromkeys((1, min(2, int(info['maxInputChannels'])))):
                    if channels < 1: continue
                    try:
                        if audio.is_format_supported(rate, input_device=selected, input_channels=channels, input_format=pyaudio.paInt16):
                            found = rate, channels; break
                    except ValueError: pass
                if found: break
            if not found: raise RuntimeError('Microphone has no supported PCM format. Choose another Windows input.')
            rate, channels = found; self.converter = FrameConverter(rate, channels)
            capture = audio.open(format=pyaudio.paInt16, channels=channels, rate=rate,
                input=True, input_device_index=selected, frames_per_buffer=rate // 50,
                stream_callback=self._capture, start=False)
            if self.stop_event.is_set(): return
            self.ws = open_socket(self.client)
            if self.stop_event.is_set(): return
            self.ws.send(json.dumps({'type': 'capabilities', 'speech': False}))
            capture.start_stream()
            for target in (self._sender, lambda: self._playback(audio)):
                thread = threading.Thread(target=target, daemon=True); thread.start(); workers.append(thread)
            self.notify('Microphone connected. Waiting for ATHENA…')
            handshake_deadline = time.monotonic() + 8
            while not self.stop_event.is_set():
                try: packet = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    if not self.ready.is_set() and time.monotonic() > handshake_deadline:
                        raise TimeoutError('Voice audio connection did not become ready.')
                    continue
                if not packet: break
                if isinstance(packet, str): self.control(json.loads(packet)); continue
                if len(packet) > 192000 or len(packet) % (self.channels * 2): raise ValueError('Invalid audio frame.')
                speech = (self.rate, self.channels) == (24000, 1)
                with self.lock:
                    generation = self.generation
                    if speech: self.speech_pending += 1
                try: self.output_frames.put_nowait((packet, self.rate, self.channels, generation))
                except queue.Full:
                    raise RuntimeError('PC playback fell behind. Check the output device and reconnect.')
            if not self.stop_event.is_set(): self.fail('Voice audio disconnected. Start the microphone again to reconnect.')
        except Exception as error:
            if not self.stop_event.is_set(): self.fail('Microphone error: ' + str(error)[:240])
        finally:
            self.close()
            if capture:
                try: capture.close()
                except Exception: pass
            for thread in workers: thread.join(timeout=2)
            if self.ws:
                try: self.ws.close(timeout=.2)
                except Exception: pass
            if audio: audio.terminate()
            self.level = (0., 0., time.monotonic())
            self.events.put(('stopped', self.failure or 'Microphone stopped.'))
