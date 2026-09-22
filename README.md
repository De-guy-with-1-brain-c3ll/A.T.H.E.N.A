# A.T.H.E.N.A.

A.T.H.E.N.A. is an open-source, JARVIS-inspired personal voice agent built in Python.
It runs continuously on a small Linux computer such as an Orange Pi while using cloud
models for conversation, speech recognition, and speech generation.

## Current features

- Wake-word voice interaction with a short follow-up listening window
- Streaming Qwen speech recognition and text-to-speech
- Optional local speech that needs no network and costs nothing per word
- Optional local recognition that costs nothing per minute
- DeepSeek conversation and tool calling
- Shared memory across voice, terminal, web, and Feishu interfaces
- Local-network control dashboard
- Feishu remote messaging
- Weather, web browsing, downloads with approval, and sandboxed coding tools
- NetEase Cloud Music playback with pause, resume, skip, stop, and volume control
- Signed local-network updates from a development computer to an Orange Pi

## Quick start

Requirements: Python 3.11 or newer, PortAudio, and FFmpeg.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
```

Fill in `.env` with your own DeepSeek and Alibaba Cloud Model Studio credentials, then
start a supported interface:

```powershell
athena-chat
```

The complete Orange Pi installation and update instructions are in
[`orange_pi/README.md`](orange_pi/README.md).

## Running without paying by the minute

Both halves of the speech pipeline can move off the cloud, and both are switched the
same way — one environment variable, with the cloud kept as the automatic fallback if
the local option is not usable.

### Speaking without paying per word

By default ATHENA speaks with the cloud voice, which is billed per character. Three
other voices replace it, and **all three are free**.

**Edge — Microsoft's neural voices, no account and no API key.**

```bash
pip install edge-tts        # ffmpeg must also be on PATH
```

Then set `ATHENA_TTS_BACKEND=edge`. Measured on an Orange Pi Zero 3: **0.84 s to first
audio and 9.6x realtime**, against the cloud voice's ~0.6 s and the local voices'
~1.1x. It is in a different league from anything that runs on the board, and it costs
nothing.

Two honest caveats. It is **the Edge browser's own protocol, reverse-engineered**, not
an official API — Microsoft can break it without notice, which is why the fallbacks
exist. And it returns **MP3**, so `ffmpeg` decodes it to PCM in a streaming subprocess
rather than after the fact.

> **Leave `ATHENA_EDGE_RATE` and `ATHENA_EDGE_PITCH` unset.** Sending a no-op `pitch`
> measured **1.2–2.0 s** to first audio against **0.85 s** when nothing was sent — a
> 2.3 second cost per reply for a parameter that changes nothing. It was the largest
> latency bug in the backend and it was invisible until measured.

**Local voices** need no network at all, and also cost nothing:

```bash
sudo bash tools/install_sherpa_tts.sh
```

Then set `ATHENA_TTS_BACKEND=sherpa`. It runs **in-process** through sherpa-onnx —
the library the local recogniser already uses — so there is no extra binary, no
subprocess, and no protocol between them to get wrong. That protocol is where
Piper's hardest bug lived.

**Which model it loads is decided by the model directory, not by the setting**,
because the right family depends on the machine — and the difference is large
enough that the desktop result reverses on the board.

| | desktop | **Orange Pi Zero 3** |
|---|---|---|
| **Edge, `en-US-AriaNeural`** | — | **0.84 s to first audio, 9.6x** |
| `vits-piper-en_US-libritts_r-medium` | 13.8x | **1.10x** |
| `vits-piper-en_US-lessac-medium` | 26x | 0.89x |
| `kitten-nano-en-v0_8-int8` | 2.6x | 0.30x |
| `kitten-nano-en-v0_8-fp32` | 11.8x | 0.46x |
| `kokoro-int8-en-v0_19` | 0.6x | — |

Note the two VITS rows: on a desktop lessac is nearly twice as fast, and on the board
LibriTTS-R is faster. **The two machines disagree about the ordering, not just the
magnitude**, which is the strongest version of the rule below — the only trustworthy
figure is one taken where the voice will run. LibriTTS-R is also 904 speakers, so
`ATHENA_SHERPA_SPEAKER` chooses the voice and 0 is only a default.

On a desktop Kitten is 2.6x; on the board it collapses to **0.30x**, where a 12.8 s
reply takes 41 s to synthesize and the speaker runs dry. The cause is the missing int8
acceleration: this CPU has NEON but no int8 dot-product, so quantised kernels fall back
to generic paths and pay far more than their size suggests — which is also why fp32
beats int8 on the board (0.46x against 0.30x) while losing badly on a desktop. **Never
extrapolate a quantised model's speed across CPUs.** Kokoro is on the list to be
rejected: at 0.6x it is slower than the speech it produces.

Two threads is the default and four is *worse* (0.89x against 0.66x), so extra
threads only take cores from capture and playback.

Speed is not the only test. Every clip was transcribed back through the local
recogniser and every fact survived — including `CJ`, which the cloud recognisers
habitually wrote as "Jesus":

> `Good morning. You have three things today. The network meeting with CJ at 430, a
> pre-calculus assignment due Friday, and the thermostat is holding at 72 degrees.`

The VITS default is also **the same lessac-medium voice the Piper install already
uses**, just in sherpa's layout — so moving to the in-process backend costs nothing
in voice quality and removes the subprocess entirely. Piper still works
(`ATHENA_TTS_BACKEND=piper`) but its `audio()` is known to hang, which is the main
reason to prefer this.

The cost with either is the **load**, not the synthesis: 7.6 s on the board. Loading
a voice blocks, so ATHENA does it in the background at start-up and keeps it for
`ATHENA_SHERPA_IDLE_SECONDS` after the last thing it said — a fresh model per reply
would make local speech the slowest option rather than the fastest.

> **The desktop figures do not transfer, and this is now measured rather than
> assumed.** On the Orange Pi, Piper's one-time voice load takes **13.3 seconds**
> and its first sentence about **2.1 seconds** to start (measured 2026-09-17). The
> local voice that actually keeps up on that board is the VITS one, at **0.89x**;
> Kitten was measured there on 2026-09-18 and manages **0.30x**, which is why it is
> not the default. See "What was actually measured on the Orange Pi" below.

### Listening without paying per minute

Recognition is billed by the **second of audio**, and at the same rate on every model
the platform offers — roughly ¥1.19 an hour of speech. That cannot be tuned by
picking a cheaper model, because there isn't one. The only lever is to stop sending
the audio anywhere.

```bash
pip install sherpa-onnx
bash tools/download_sensevoice.sh
```

Then set `ATHENA_STT_BACKEND=sensevoice`. This runs SenseVoice-Small on the machine
itself: no API key, no network, and nothing to pay.

The interesting part is that SenseVoice is **not** a streaming model: it wants a
finished utterance and returns one answer, while ATHENA's recognition interface is
built around audio arriving in 100 ms packets with partial transcripts as you speak.
The bridge is to keep the turn's audio in a buffer and re-decode it as more arrives,
on a background thread, so the partials *replace* each other the way the cloud ones
do instead of piling up.

Accuracy is close to the cloud models (word error rate 0.188 against 0.112 on
benchmark clips), and it has one real advantage: on desktop hardware it gets domain
words right that the cloud recognisers miss. `The meeting with CJ is at four thirty
this afternoon.` comes back correctly from SenseVoice where the cloud models wrote
"Jesus". The `int8` model is the default — 228 MB rather than 938 MB, and about 2.5x
faster than `fp32`.

### What was actually measured on the Orange Pi

The desktop numbers above do not transfer to the Pi Zero 3, and the difference is
large enough to change the decision. Both halves were installed and measured on the
board, Piper and SenseVoice on 2026-09-17 and the sherpa voices on 2026-09-18:

| | value on the Pi | needed for real time |
|---|---|---|
| **VITS Piper voice, 2 threads** | **0.89x realtime** | ≥ 1x, ideally |
| VITS Piper voice, 4 threads | 0.66x realtime | ≥ 1x |
| VITS Piper voice load | **7.6 s** at start-up, once | one-time |
| Kitten nano int8, 2 threads | 0.30x realtime | ≥ 1x |
| Kitten nano fp32, 2 threads | 0.46x realtime | ≥ 1x |
| SenseVoice decode, 2 threads | **1.1x realtime** | ≥ 1x, comfortably |
| SenseVoice decode, 4 threads | **0.7x realtime** | ≥ 1x |
| SenseVoice model load | ~12.5 s at start-up | one-time |
| Piper (subprocess), time to first audio | **~2.1 s** | as low as possible |
| Piper (subprocess) voice load | **13.3 s** at start-up, once | one-time |
| Piper (subprocess) raw graph, 2 threads | **1.02x realtime** | ≥ 1x |
| Piper (subprocess) raw graph, 4 threads | **1.39x realtime** | ≥ 1x |

The in-process VITS voice is the one to use: it is the same lessac-medium voice the
subprocess Piper already had, at 0.89x rather than 1.02x, and it removes the
subprocess and its hang. Kitten is **not** viable here in either precision — 0.30x
means a 12.8 s reply takes 41 s to synthesize. Note that fp32 beats int8 on this
board (0.46x against 0.30x) while losing badly on a desktop, which is the int8
fallback showing up.

This board's CPU has NEON but **no int8 dot-product extensions** (`dotprod`/`i8mm`), so
quantised models fall back to slow generic kernels. Expect any `int8` model here to be
well behind its desktop number — Kitten lost 9x, not the 2x that core count and clock
speed would suggest.

The conclusion for this board: **local speech is a cost saving, not a latency win.**
It removes the per-character and per-second bills, and it removes the network
dependency, but the local voices still pause noticeably before they speak.

**Local recognition also costs CPU, and the cloud one costs none.** The recogniser
re-decodes the whole turn's buffer every 0.8 s to emit partials, and at 1.1x realtime
a buffer the voice gate holds open can never be decoded in time. That was measured at
**326% CPU** on this 4-core board, which starved everything else on it — a release
build that normally takes 2 min 40 s exceeded a 15-minute timeout. Idle, with the gate
correctly reporting silence, it settles near 50%. So the trap is a voice gate that
sticks on room noise rather than the idle path; watch for `speech=yes` in the log
after enabling it.

**The Pi runs a split: local voice, cloud recognition.** `ATHENA_TTS_BACKEND=sherpa`
(VITS) stays local because it is free and keeps up at 0.89x. `ATHENA_STT_BACKEND` is
back to `qwen`, because **the local recogniser's latency made it unusable in practice**
— the reply to an earlier request already had the answer.

That is the honest outcome of trying both. Local recognition is accurate and free, and
on this board it is not fast enough to talk to: partials arrive late, the final
transcript lands after the pause a person reads as "it didn't hear me", and it holds
CPU that the rest of the system wants. Everything needed to switch it back is
installed and one value in `/etc/athena/athena.env`; the fallback logic means a wrong
answer degrades to the cloud instead of leaving ATHENA deaf.

## Security

- Never commit `.env`, `API KEY.txt`, update signing keys, databases, or downloaded files.
- Downloads and local command execution require explicit user approval.
- The dashboard accepts local-network connections only and requires authentication.
- Website text and tool output are treated as untrusted data.

If a credential has ever been published, rotate it immediately; deleting it from the
latest commit is not enough because Git retains history.

## Tests

```powershell
python -m unittest discover -s tests
```

## Working on ATHENA without the Orange Pi

Everything can be developed and tested on a normal computer. On Windows, use WSL:

```bash
bash tools/setup_wsl.sh
```

That installs the system packages, builds the virtual environment, checks that
bubblewrap can sandbox generated code, and runs the tests.

There is then a local harness that runs the **real** tool registry, alarm
scheduler, voice gate and tool loop with fake cloud providers — no credentials,
no cost, no network. Only speech recognition, the model and speech synthesis are
replaced, and everything uses a throwaway data directory, so your real alarms,
memory and settings are never touched:

```bash
python -m athena.dev.harness alarms   # speak an alarm and watch it actually fire
python -m athena.dev.harness vad      # see the voice gate's decisions frame by frame
python -m athena.dev.harness chat     # type to ATHENA offline; alarms still fire
```

For a real conversation with the real model, `athena-chat` needs only
`DEEPSEEK_API_KEY`; it uses the same tools, memory and approvals as voice mode.

### Measuring speech recognition locally

Both recognisers can be measured without the Pi and without spending anything.

```bash
python tools/bench_stt_latency.py --record 5      # capture your own voice, then test it
python tools/bench_stt_latency.py --file clip.wav --plain
python tools/bench_stt.py                         # every cloud ASR model (needs an API key)
python tools/bench_sensevoice.py                  # local SenseVoice, accuracy and raw speed
```

The test clips are **not** in the repository — `outputs/` is gitignored — so
`bench_stt.py` generates them from the cloud voice on first use, which needs
`DASHSCOPE_API_KEY`. Everything after that is free and offline. If you just want
to know how local recognition performs, skip that step entirely and record your
own audio:

```bash
python tools/bench_stt_latency.py --record 5      # needs: pip install sounddevice
```

`bench_stt_latency.py` is the one that answers "how long until ATHENA hears me".
It feeds audio through the **actual** `SenseVoiceRecognizer` in the same 100 ms
packets the microphone produces, and reports three numbers a whole-clip benchmark
cannot see: time to the first partial transcript, how many partials it emits, and
the finalisation latency — the pause after you stop talking before a reply can
begin. That last one is what a conversation actually feels like.

Stereo and 44.1 kHz recordings are converted automatically, so a clip from a phone
works as-is. `--plain` prints just the transcript, which is the useful shape when
the question is simply "did it hear me right".

Measured on this machine against the generated clips (int8 model, two threads,
100 ms packets):

| clip | audio | first partial | finalisation | WER |
|---|---|---|---|---|
| cj | 2.57 s | 0.88 s | 0.19 s | 0.20 |
| numbers | 4.10 s | 0.90 s | 0.28 s | 0.07 |
| courses | 3.70 s | 0.89 s | 0.41 s | 0.14 |
| long | 10.76 s | 0.92 s | 1.00 s | 0.08 |

Time to the first partial is essentially constant at **~0.9 s** regardless of how
long you speak, because it is set by the 0.8 s partial interval rather than by the
audio length. Finalisation latency is what grows with the sentence, and it stays
under a second until the sentence gets long.

### Measuring speech synthesis locally

Choosing between the cloud voice and the local ones is a benchmark, not a guess.

```bash
python tools/bench_tts_local.py                        # every candidate, 2 threads
python tools/bench_tts_local.py --models kitten --threads 4 --repeats 3
python tools/bench_tts_local.py --models kitten,kokoro,piper --threads 2
python tools/bench_tts_local.py --wavs                 # also write the audio out
```

Each engine is measured **twice** — once cold, including its model load, and once
reused — because those describe different things. A per-sentence number that includes
a load is measuring the load, not the synthesis, which is exactly the mistake that made
an earlier Piper figure wrong. Only the reused figure describes a conversation.

Candidates live in `outputs/tts-candidates/` (gitignored) and are fetched by the
installer scripts. Measured on this machine, 2 threads, median of three:

| engine | model | desktop | Orange Pi |
|---|---|---|---|
| vits-piper lessac-medium | 63 MB | 26x | **0.89x** |
| kitten-nano int8 | 24 MB | 2.6x | 0.30x |
| kitten-nano fp32 | 75 MB | 11.8x | 0.46x |
| kokoro int8 | 134 MB | 0.6x | — |

**Run this on the machine that will speak.** The desktop and the board disagree by up
to 30x and in both directions, so the only trustworthy figure is the one taken where
the voice will run. Kokoro is listed to be rejected rather than chosen: below 1x
realtime means synthesis takes longer than the speech it produces, so its queue never
drains. `tools/install_sherpa_tts.sh` installs a voice on a Pi.

Speed is only half the question. Every generated clip was transcribed back through the
local recogniser to confirm the words survive the round trip — `/tmp`-grade proof that
audio is intelligible rather than merely fast:

```bash
python tools/bench_stt_latency.py --file outputs/audio-tests/tts-local/kitten-long.wav --plain
```

## License

MIT. See [`LICENSE`](LICENSE).
