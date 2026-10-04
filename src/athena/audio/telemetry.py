from __future__ import annotations

import json
import os
from pathlib import Path
import time


DEFAULT_STATUS_PATH = Path("/run/athena/audio-status.json")

WAITING = "waiting"
LISTENING = "listening"
SPEECH = "speech"
ENDED = "ended"


def read_status(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


class AudioStatusWriter:
    """Publish microphone and speech state to tmpfs, never the SD card."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._payload: dict = {}

    @classmethod
    def from_environment(cls) -> "AudioStatusWriter":
        configured = os.environ.get("ATHENA_AUDIO_STATUS_PATH", "").strip()
        return cls(Path(configured) if configured else None)

    def update(self, *, rms: float, noise: float, threshold: float,
               speech: bool, voiced_frames: int,
               retained_frames: int | None = None,
               retained_seconds: float | None = None,
               dropped_frames: int | None = None,
               dropped_seconds: float | None = None) -> None:
        fields = {
            "rms": round(rms, 1),
            "noise": round(noise, 1),
            "threshold": round(threshold, 1),
            "speech": speech,
            "voiced_frames": voiced_frames,
        }
        if retained_frames is not None:
            fields["retained_frames"] = retained_frames
        if retained_seconds is not None:
            fields["retained_seconds"] = round(retained_seconds, 3)
        if dropped_frames is not None:
            fields["dropped_frames"] = dropped_frames
        if dropped_seconds is not None:
            fields["dropped_seconds"] = round(dropped_seconds, 3)
        self._publish(**fields)

    def state(self, value: str, *, turn: str = "") -> None:
        fields: dict = {"state": value}
        if turn:
            fields["turn"] = turn
        self._publish(**fields)

    def heard(self, text: str) -> None:
        """The partial transcript, replaced as recognition improves it."""
        self._publish(heard=text, heard_at=time.time())

    def transcript(self, text: str) -> None:
        self._publish(transcript=text, transcript_at=time.time())

    def endpoint(self, *, turn: str = "", frames: int = 0, seconds: float = 0.0,
                 dropped_frames: int = 0, dropped_seconds: float = 0.0) -> None:
        """End of speech: what was retained from onset, and what was thrown away."""
        count = int(self._payload.get("eos_count", 0)) + 1
        self._publish(
            state=ENDED,
            eos_count=count,
            eos_at=time.time(),
            eos_turn=turn,
            retained_frames=frames,
            retained_seconds=round(seconds, 3),
            dropped_frames=dropped_frames,
            dropped_seconds=round(dropped_seconds, 3),
        )

    def retention(self, *, frames: int, seconds: float,
                  dropped_frames: int = 0, dropped_seconds: float = 0.0) -> None:
        self._publish(
            retained_frames=frames,
            retained_seconds=round(seconds, 3),
            dropped_frames=dropped_frames,
            dropped_seconds=round(dropped_seconds, 3),
        )

    def _publish(self, **fields) -> None:
        self._payload.update(fields)
        self._flush()

    def _flush(self) -> None:
        if self.path is None:
            return
        payload = dict(self._payload)
        payload["updated"] = time.time()
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(payload, separators=(",", ":")),
                                 encoding="utf-8")
            temporary.replace(self.path)
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


def format_speech(status: dict) -> str:
    heard = str(status.get("heard", ""))
    final = str(status.get("transcript", ""))
    return (
        f"state {str(status.get('state', WAITING)):9s} "
        f"eos {int(status.get('eos_count', 0)):3d}  "
        f"retained {float(status.get('retained_seconds', 0)):6.2f}s "
        f"dropped {float(status.get('dropped_seconds', 0)):5.2f}s  "
        f"| heard: {heard[:60] or '-'}  | final: {final[:60] or '-'}"
    )


def monitor(path: Path = DEFAULT_STATUS_PATH, refresh: float = 0.1) -> None:
    print("ATHENA live microphone monitor — press Ctrl+C to stop watching.")
    try:
        while True:
            try:
                status = json.loads(path.read_text(encoding="utf-8"))
                age = time.time() - float(status.get("updated", 0))
                if age >= 2:
                    line = "ATHENA audio data is stale; checking service..."
                else:
                    line = format_status(status) + "  " + format_speech(status)
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
