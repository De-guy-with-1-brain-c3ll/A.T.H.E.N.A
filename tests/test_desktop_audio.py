from array import array
import json
import queue
import threading
import unittest
from unittest.mock import MagicMock, patch

try:
    from athena.desktop import PinnedConnection
except ImportError:
    PinnedConnection = None
from athena.desktop_audio import FrameConverter, NativeAudio, levels, open_socket


class PCMTests(unittest.TestCase):
    def test_silence_and_clipping_meter(self):
        self.assertEqual(levels(bytes(640)), (0., 0))
        self.assertEqual(levels(array('h', [-32768, 32767]).tobytes())[1], 32768)
        self.assertEqual(levels(b''), (0, 0))

    def test_native_16k_frames_are_exact_and_buffered(self):
        converter = FrameConverter(16000)
        self.assertEqual(converter.convert(array('h', [100] * 100).tobytes()), [])
        packets = converter.convert(array('h', [100] * 220).tobytes())
        self.assertEqual(packets, [array('h', [100] * 320).tobytes()])

    def test_stereo_is_mixed_to_mono(self):
        converter = FrameConverter(16000, 2)
        packets = converter.convert(array('h', [3000, -1000] * 320).tobytes())
        self.assertEqual(array('h', packets[0]), array('h', [1000] * 320))

    def test_resampling_48k_and_44k_does_not_drift_or_grow(self):
        for rate in (48000, 44100):
            converter = FrameConverter(rate)
            total = 0
            for _ in range(250):
                packets = converter.convert(array('h', [1234] * (rate // 50)).tobytes())
                total += len(packets) * 320
                for packet in packets: self.assertEqual(len(packet), 640)
            self.assertLessEqual(abs(total - 80000), 320)
            self.assertLess(len(converter.pending), 320)
            self.assertLess(len(converter.samples), 4)


class NativeAudioTests(unittest.TestCase):
    def make_audio(self):
        audio = NativeAudio(MagicMock())
        audio.converter = FrameConverter(16000); audio.ws = MagicMock()
        return audio

    def test_capture_standby_meters_but_does_not_forward(self):
        audio = self.make_audio()
        audio._capture(array('h', [1200] * 320).tobytes(), 320, None, 0)
        self.assertAlmostEqual(audio.level[0], 1200)
        self.assertTrue(audio.input_frames.empty())

    def test_microphone_stays_live_during_reply_for_stop_commands(self):
        audio = self.make_audio(); audio.route_active = True; audio.speech_pending = 1
        audio._capture(array('h', [1200] * 320).tobytes(), 320, None, 0)
        self.assertEqual(len(audio.input_frames.get_nowait()), 640)
        self.assertEqual(audio.level[0], 1200)
        audio.speech_pending = 0
        audio._capture(bytes(640), 320, None, 0)
        self.assertEqual(len(audio.input_frames.get_nowait()), 640)

    def test_capture_queue_is_bounded_and_drops_old_not_new(self):
        audio = self.make_audio(); audio.route_active = True
        for number in range(20):
            audio._capture(array('h', [number] * 320).tobytes(), 320, None, 0)
        self.assertEqual(audio.input_frames.qsize(), 8)
        self.assertEqual(array('h', audio.input_frames.get_nowait())[0], 12)

    def test_formats_flush_and_capabilities(self):
        audio = self.make_audio()
        audio.control({'type': 'ready', 'speaker_rate': 24000, 'speaker_channels': 1})
        self.assertTrue(audio.ready.is_set())
        audio.control({'type': 'audio_format', 'rate': 48000, 'channels': 2})
        self.assertEqual((audio.rate, audio.channels), (48000, 2))
        with self.assertRaises(ValueError): audio.control({'type': 'audio_format', 'rate': 123, 'channels': 9})
        audio.input_frames.put(bytes(640)); audio.control({'type': 'flush'})
        self.assertTrue(audio.input_frames.empty())
        audio.control({'type': 'speak', 'text': 'test'})
        self.assertEqual(json.loads(audio.ws.send.call_args.args[0]), {'type': 'speak_done', 'ok': False})

    def test_stop_unblocks_connection_and_capture(self):
        audio = self.make_audio(); audio.close()
        self.assertTrue(audio.stop_event.is_set())
        audio.ws.shutdown.assert_called_once()

    def test_disconnect_releases_capture_without_starting_speaker(self):
        audio = self.make_audio(); backend = MagicMock()
        backend.get_default_input_device_info.return_value = {
            'index': 0, 'defaultSampleRate': 48000, 'maxInputChannels': 1}
        backend.is_format_supported.return_value = True
        ws = MagicMock(); ws.recv.side_effect = [json.dumps({'type': 'ready'}), '']
        with patch('pyaudio.PyAudio', return_value=backend), patch('athena.desktop_audio.open_socket', return_value=ws):
            audio._run()
        backend.open.assert_called_once()
        self.assertTrue(backend.open.call_args.kwargs['input'])
        backend.open.return_value.close.assert_called_once()
        backend.terminate.assert_called_once()
        self.assertTrue(audio.stop_event.is_set())
        events = []
        while not audio.events.empty(): events.append(audio.events.get_nowait())
        self.assertIn('disconnected', events[-1][1])

    def test_sender_uses_raw_binary_frames(self):
        audio = self.make_audio(); audio.route_active = True; audio.input_frames.put(bytes(640))
        audio.ws.send_binary.side_effect = lambda data: audio.stop_event.set()
        audio._sender(); audio.ws.send_binary.assert_called_once_with(bytes(640))

    @unittest.skipIf(PinnedConnection is None, 'Desktop runtime unavailable')
    def test_websocket_pin_checked_before_any_cookie_is_sent(self):
        client = MagicMock(); client.host = '192.168.33.153'; client.fingerprint = 'pin'; client.cookie = 'secret session'
        with patch('athena.desktop.PinnedConnection') as connection, patch('websocket.create_connection') as create:
            connection.return_value.connect.side_effect = OSError('certificate changed')
            with self.assertRaises(OSError): open_socket(client)
            create.assert_not_called()

    def test_playback_fake_device_uses_format_and_can_be_cancelled(self):
        audio = self.make_audio(); backend = MagicMock(); stream = backend.open.return_value
        audio.output_frames.put((bytes(1920), 48000, 2, audio.generation))
        stream.write.side_effect = lambda data, **kw: audio.stop_event.set()
        audio._playback(backend)
        self.assertEqual(backend.open.call_args.kwargs['rate'], 48000)
        self.assertEqual(backend.open.call_args.kwargs['channels'], 2)
        stream.close.assert_called_once()
