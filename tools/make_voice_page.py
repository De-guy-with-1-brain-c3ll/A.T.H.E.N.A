"""Build a single page for listening to and comparing every voice.

The measurements decide cost and latency; the ear decides quality, and no table
can carry that. This writes the samples to one page with the numbers beside them.

    ./.venv/Scripts/python.exe tools/make_voice_page.py
"""
from __future__ import annotations

import base64
import json
from pathlib import Path


def embed(path: Path) -> str:
    return "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode()


def main() -> int:
    cloud = Path("outputs/audio-tests/voices")
    piper = Path("outputs/audio-tests/piper")
    out = Path("outputs/audio-tests/compare.html")

    tts = json.loads(Path("outputs/audio-tests/tts-probe.json").read_text())
    latency = {row["voice"]: row for row in tts["latency"]}

    rows = []
    for voice in tts["voices_accepted"]:
        clip = cloud / f"{voice}.wav"
        if not clip.is_file():
            continue
        stat = latency.get(voice, {})
        rows.append({
            "name": voice, "engine": "Qwen cloud",
            "first": f"{stat.get('first_median', 0):.2f}s",
            "cost": "~CNY 1.19 / hour of speech",
            "audio": embed(clip),
        })

    for clip in sorted(piper.glob("*/*.wav")):
        if clip.stem != "plain":
            continue
        rows.append({
            "name": clip.parent.name.replace("en_US-", ""),
            "engine": "Piper local",
            "first": "~2.0s (model load)",
            "cost": "free",
            "audio": embed(clip),
        })

    cards = []
    for row in rows:
        cards.append(f"""
    <div class="card">
      <div class="top">
        <span class="name">{row['name']}</span>
        <span class="engine">{row['engine']}</span>
      </div>
      <div class="meta">
        <span>first audio <b>{row['first']}</b></span>
        <span>{row['cost']}</span>
      </div>
      <audio controls preload="none" src="{row['audio']}"></audio>
    </div>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ATHENA voice comparison</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0;
         padding: 32px; background: #faf9f5; color: #2c2c2a; }}
  h1 {{ font-size: 20px; font-weight: 500; margin: 0 0 4px; }}
  p.lede {{ color: #5f5e5a; font-size: 13px; margin: 0 0 24px; max-width: 60ch; }}
  .grid {{ display: grid; gap: 12px;
           grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }}
  .card {{ background: #fff; border: 1px solid rgba(0,0,0,.12);
           border-radius: 12px; padding: 14px 16px; }}
  .top {{ display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; }}
  .name {{ font-weight: 500; font-size: 14px; }}
  .engine {{ font-size: 12px; color: #888780; }}
  .meta {{ display: flex; gap: 14px; font-size: 12px; color: #5f5e5a;
           margin-bottom: 10px; }}
  .meta b {{ font-weight: 500; color: #2c2c2a; }}
  audio {{ width: 100%; height: 34px; }}
</style>
</head>
<body>
  <h1>ATHENA voice comparison</h1>
  <p class="lede">The same sentence spoken by every available voice. Latency is the
  median time to first audio measured over four runs; the cloud voices are billed
  per character and Piper is free.</p>
  <div class="grid">{''.join(cards)}
  </div>
</body>
</html>
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"{len(rows)} voices -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
