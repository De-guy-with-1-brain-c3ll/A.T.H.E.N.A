import json
from pathlib import Path
import tempfile
import unittest

from athena.audio.telemetry import AudioStatusWriter, format_status


class AudioTelemetryTests(unittest.TestCase):
    def test_writer_publishes_machine_readable_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.json"
            AudioStatusWriter(path).update(
                rms=723.4, noise=112.2, threshold=400,
                speech=True, voiced_frames=9,
            )
            status = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(status["rms"], 723.4)
            self.assertTrue(status["speech"])
            self.assertEqual(status["voiced_frames"], 9)

    def test_live_line_shows_level_threshold_and_state(self):
        line = format_status({
            "rms": 800, "noise": 120, "threshold": 400,
            "speech": True, "voiced_frames": 12,
        })
        self.assertIn("MIC [", line)
        self.assertIn("threshold", line)
        self.assertIn("SPEECH", line)
        self.assertIn("frames", line)


if __name__ == "__main__":
    unittest.main()
