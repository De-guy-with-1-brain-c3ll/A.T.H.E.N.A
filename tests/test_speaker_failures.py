import unittest
from unittest.mock import MagicMock, patch

from athena.audio.playback import Speaker


class SpeakerLoudnessTests(unittest.IsolatedAsyncioTestCase):
    """A speaker that cannot play must say so.

    The Pi board has no guaranteed sound hardware, and the browser route can be
    absent at any moment. When that happened the audio was dropped on the floor
    without a word, so the player reported a track as playing and the room
    stayed silent. Both the unopened stream and a failed open now raise.
    """

    async def test_playing_before_open_is_an_error_not_silence(self):
        speaker = Speaker()
        with self.assertRaisesRegex(RuntimeError, "not open"):
            await speaker.play(b"\x00\x00" * 100)

    async def test_a_failed_open_names_the_available_outputs(self):
        fake = MagicMock()
        fake.get_default_output_device_info.side_effect = OSError("no default device")
        fake.get_device_count.return_value = 2
        fake.get_device_info_by_index.side_effect = [
            {"index": 0, "name": "HDMI", "maxOutputChannels": 2},
            {"index": 1, "name": "USB Audio", "maxOutputChannels": 2},
        ]
        speaker = Speaker()
        with patch("athena.audio.playback.pyaudio.PyAudio", return_value=fake):
            with self.assertRaises(RuntimeError) as caught:
                await speaker.open()
        message = str(caught.exception)
        self.assertIn("HDMI", message)
        self.assertIn("USB Audio", message)
        self.assertIn("ATHENA_AUDIO_OUTPUT_DEVICE", message)
        # The half-built audio object must not be left holding the device.
        fake.terminate.assert_called_once()
        self.assertIsNone(speaker._audio)

    async def test_a_board_with_no_output_device_says_so(self):
        fake = MagicMock()
        fake.get_default_output_device_info.side_effect = OSError("no default device")
        fake.get_device_count.return_value = 0
        speaker = Speaker()
        with patch("athena.audio.playback.pyaudio.PyAudio", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "no audio output devices"):
                await speaker.open()

    async def test_a_named_device_that_does_not_exist_is_reported(self):
        speaker = Speaker(device="Nonexistent Device")
        fake = MagicMock()
        fake.get_device_count.return_value = 1
        fake.get_device_info_by_index.return_value = {
            "index": 0, "name": "HDMI", "maxOutputChannels": 2}
        with patch("athena.audio.playback.pyaudio.PyAudio", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                await speaker.open()

    async def test_opened_speaker_still_plays_normally(self):
        speaker = Speaker()
        written = []

        class Stream:
            def write(self, data): written.append(data)

        speaker._stream = Stream()
        await speaker.play(b"\x01\x02\x03\x04")
        self.assertEqual(b"".join(written), b"\x01\x02\x03\x04")