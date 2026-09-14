from __future__ import annotations

import json
import os
from pathlib import Path
import time


DEFAULT_STATUS_PATH = Path("/run/athena/audio-status.json")


class AudioStatusWriter:
    """Publish fast-changing microphone state to tmpfs, never the SD card."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    @classmethod
    def from_environment(cls) -> "AudioStatusWriter":
        configured = os.environ.get("ATHENA_AUDIO_STATUS_PATH", "").strip()
        return cls(Path(configured) if configured else None)

    def update(self, *, rms: float, noise: float, threshold: float,
               speech: bool, voiced_frames: int) -> None:
        if self.path is None:
            return
        payload = {
            "rms": round(rms, 1),
            "noise": round(noise, 1),
            "threshold": round(threshold, 1),
            "speech": speech,
            "voiced_frames": voiced_frames,
            "updated": time.time(),
        }
        try:
            self.path.write_text(json.dumps(payload, separators=(",", ":")),
                                 encoding="utf-8")
        except OSError:
            # Audio capture must never fail because the optional display vanished.
            pass


def format_status(status: dict, width: int = 36) -> str:
    rms = max(0.0, float(status.get("rms", 0)))
    threshold = max(1.0, float(status.get("threshold", 1)))
    scale = max(threshold * 2.0, rms, 1.0)
    filled = min(width, round(rms / scale * width))
    marker = min(width - 1, round(threshold / scale * width))
    cells = ["#" if index < filled else "-" for index in range(width)]
    if marker >= filled:
        cells[marker] = "|"
    state = "SPEECH" if status.get("speech") else "waiting"
    return (
        f"MIC [{''.join(cells)}] {rms:6.0f}  "
        f"threshold {threshold:5.0f}  noise {float(status.get('noise', 0)):5.0f}  "
        f"{state:7s}  frames {int(status.get('voiced_frames', 0)):4d}"
    )


def monitor(path: Path = DEFAULT_STATUS_PATH, refresh: float = 0.1) -> None:
    print("ATHENA live microphone monitor — press Ctrl+C to stop watching.")
    try:
        while True:
            try:
                status = json.loads(path.read_text(encoding="utf-8"))
                age = time.time() - float(status.get("updated", 0))
                line = format_status(status) if age < 2 else "ATHENA audio data is stale; checking service..."
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                line = "Waiting for ATHENA voice service and microphone data..."
            print("\r\033[2K" + line, end="", flush=True)
            time.sleep(refresh)
    except KeyboardInterrupt:
        print("\nMonitor closed. ATHENA is still running.")


def main() -> int:
    configured = os.environ.get("ATHENA_AUDIO_STATUS_PATH", "").strip()
    monitor(Path(configured) if configured else DEFAULT_STATUS_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
