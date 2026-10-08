"""Capture-only hardware check: no recordings, network, recognition or playback."""
import time
import pyaudio
from athena.desktop_audio import levels


def main():
    audio = pyaudio.PyAudio(); capture = None; samples = []
    try:
        info = audio.get_default_input_device_info()
        def on_frame(pcm, count, timing, status):
            samples.append(levels(pcm)[0]); return None, pyaudio.paContinue
        capture = audio.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True,
            input_device_index=int(info['index']), frames_per_buffer=320, stream_callback=on_frame)
        time.sleep(3)
        capture.stop_stream()
        if not samples: raise RuntimeError('No microphone frames arrived.')
        print(f'PASS: Windows default microphone delivered {len(samples)} live meter updates.')
        print(f'RMS range: {min(samples):.0f}–{max(samples):.0f}. No network, speech recognition, recordings or playback used.')
    finally:
        if capture: capture.close()
        audio.terminate()


if __name__ == '__main__': main()
