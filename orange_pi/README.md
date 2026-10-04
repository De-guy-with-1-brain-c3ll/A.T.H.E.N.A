# ATHENA for Orange Pi Zero 3 (4 GB)

This folder is the Orange Pi deployment version. ATHENA still uses cloud DeepSeek,
Qwen STT/TTS, Feishu, weather, and web services; the Pi runs the microphone, speaker,
memory, tools, and coordinator. That keeps RAM use low and preserves the same assistant.

The recommended OS is a 64-bit Armbian or Debian command-line image. Do not install a
desktop environment. The service limits ATHENA to 1 GB in Feishu mode or 1.2 GB in
voice mode. Chromium rendering is optional; ordinary web search and webpage reading do
not require Chromium.

## How updates work

```text
Windows source -> signed ZIP feed -> local network -> Orange Pi updater
                                                    -> verify signature
                                                    -> build new isolated release
                                                    -> switch current link
                                                    -> restart ATHENA
                                                    -> roll back if restart fails
```

The update key never travels over the network. It signs each build with HMAC-SHA256,
so another device on the network cannot replace ATHENA with modified code. Releases
are installed under `/opt/athena/releases`; memory and user data remain separately in
`/opt/athena/data`.

## 1. Copy this folder and the helper scripts to the Pi once

From PowerShell on the computer, replace the address with the Pi's address:

```powershell
scp -r .\orange_pi .\tools orangepi@192.168.1.50:/tmp/
```

`tools` is copied too because the optional local-voice installers run on the Pi, and
the signed update feed only carries `src/athena` — so nothing else would ever put them
there.

On the Pi:

```bash
cd /tmp/orange_pi
sudo bash pi/install.sh
```

The installer adds Python, PortAudio, ALSA development files, and Bubblewrap. It creates
an unprivileged `athena` service account. It does not start ATHENA until configuration
and a signed release exist.

It also installs lightweight BlueALSA support for Bluetooth headset microphones. The
included BlueALSA service profile enables A2DP playback plus HFP/HSP call audio, which
is required to expose a paired headset microphone to ALSA on a headless Pi.

## 2. Configure ATHENA on the Pi

```bash
sudo nano /etc/athena/athena.env
sudo nano /etc/athena/update.env
```

Put the DeepSeek, DashScope, and Feishu credentials in `athena.env`. Set
`ATHENA_UPDATE_URL` in `update.env` to the feed URL printed by the computer. Copy the
update key printed by the computer into `ATHENA_UPDATE_KEY`. Never put this key in Git
or a Feishu message.

If the key was generated earlier and is no longer visible, it is stored locally at
`orange_pi\.update-key` on the computer.

### Optional: speak locally instead of paying per word

The cloud voice is billed per character. Local voices run on the board itself, so they
are free and need no network. One command installs one, verifies it can speak, and
prints the settings to add to `athena.env`:

```bash
sudo bash /tmp/tools/install_sherpa_tts.sh    # recommended
sudo bash /tmp/tools/install_piper.sh         # the earlier attempt
```

**Use the default voice the installer picks.** It is
`vits-piper-en_US-lessac-medium` — the same lessac-medium voice the Piper install
already uses, but in sherpa's layout, so it runs **in-process** through sherpa-onnx and
there is no separate binary, no subprocess, and none of the turn-end protocol where
Piper's hang lives. Voice quality is therefore unchanged and the subprocess disappears.

**Measured on this board on 2026-09-18, and the family matters more than anything else:**

| voice | realtime | load |
|---|---|---|
| `vits-piper-en_US-lessac-medium` | **0.89x** | 7.6 s |
| `kitten-nano-en-v0_8-int8` | 0.30x | 5.5 s |
| `kitten-nano-en-v0_8-fp32` | 0.46x | 5.0 s |

**Kitten is not viable here.** 0.30x means a 12.8 second reply takes 41 seconds to
synthesize, so the speaker runs dry before the sentence ends — and it is the model that
looks fastest on a desktop (2.6x), which is why this had to be measured rather than
assumed. The cause is this CPU having NEON but **no int8 dot-product extensions**, so
quantised kernels fall back to generic paths; that is also why fp32 beats int8 here
while losing badly on a desktop. The installer warns if the voice it just installed is
slower than realtime on this board.

Two threads is right; four is worse (0.66x against 0.89x). Both are installed with the
same command — pass the model name to try another.

Piper's own numbers are kept for reference: its graph runs at about **1.02x realtime on
two threads and 1.39x on four**, but its **one-time voice load takes 13.3 seconds** and
its first sentence waits about **2.1 seconds**. That is usable, and better than it first
appeared, but the in-process voice is preferred.

### Optional: listen locally instead of paying per minute

Recognition is billed per second of audio at the same rate on every model the platform
offers, so there is no cheaper model to switch to. SenseVoice-Small runs on the board
instead: free, and with no network at all.

```bash
sudo bash /tmp/tools/download_sensevoice.sh
```

**Read this before enabling it, and read the second paragraph especially.** SenseVoice
was installed and measured on this board on 2026-09-17: **1.1x realtime on two threads
and 0.7x on four**, with a ~12.5 second model load at start-up. It is accurate — it
transcribed `The meeting with CJ is at four thirty this afternoon.` correctly, including
the "CJ" the cloud models miss — but at around real time it only just keeps pace while
someone is speaking, and the partial transcripts arrive late. Four threads was *slower*
than two, so the decode is memory-bandwidth-bound and adding cores does not help.

**It also costs CPU, which the cloud recogniser does not — and on this board that is
the part that bites.** The recogniser re-decodes the whole turn's buffer every 0.8 s so
the partials replace each other, and at 1.1x realtime a buffer the voice gate holds open
can never be decoded in time. Measured on 2026-09-18: **326% CPU** on this 4-core board,
which starved everything else running on it — a release build that normally takes
2 min 40 s exceeded a 15-minute timeout while this was running. Idle, with the gate
correctly reporting silence, it settles near 50%. So the failure is a voice gate that
sticks on room noise, not the idle path: after enabling, watch for `speech=yes` in
`journalctl -u athena-voice` and check the process's CPU. If the gate sticks, raise
`ATHENA_VAD_MINIMUM_RMS`.

**Tried on this board, and switched back off — the latency makes it unusable to talk
to.** Local recognition is accurate and free, and at 1.1x realtime the partials arrive
late and the final transcript lands after the pause a person reads as "it didn't hear
me". It also holds CPU the rest of the system wants: a stuck voice gate was measured at
**326%** on this 4-core board. The Pi therefore runs `ATHENA_STT_BACKEND=qwen` with the
local voice still on, and the model stays installed for anyone who wants it on faster
hardware or for offline use.

The measurements above are kept because they are the reason for that decision. Either
backend can still be turned on by changing one value in `/etc/athena/athena.env`
and restarting; everything needed is installed. If a backend is requested and is not
usable, ATHENA falls back to the cloud and prints the reason rather than going mute or
deaf, so trying it cannot leave the assistant broken.

## 3. Start automatic publishing on the computer

From the ATHENA project in PowerShell:

```powershell
.\.venv\Scripts\python.exe .\orange_pi\pc\dev_update_server.py
```

You can also double-click `orange_pi\Start Pi Update Server.cmd`.

The first run prints the Pi feed URL and a newly generated update key. It then watches
`src/athena`, `pyproject.toml`, `.env.example`, and `orange_pi/VERSION`. Saving a code
change automatically publishes a new signed release. Keep this window running while
developing. Windows may ask you to permit Python on private networks; private networks
only is sufficient.

### When the Pi's address changes

The router hands out a new address when the lease changes, and both the helper
scripts and the Pi's own `ATHENA_UPDATE_URL` then point at a machine that is no
longer there. When the Pi moves:

```powershell
# 1. find it (its hostname is athena-pi)
arp -a | findstr 192.168
# 2. record it once; every helper script reads this file
notepad orange_pi\pi-address.txt
```

Then fix the Pi's side, using the computer's current address:

```bash
sudo nano /etc/athena/update.env     # ATHENA_UPDATE_URL=http://COMPUTER_IP:8765/
sudo systemctl start athena-update.service
```

The `.cmd` helpers read `pi-address.txt`, so they follow the change without edits.
If the Pi keeps moving, give it a DHCP reservation in the router.

## 4. Pull the first release on the Pi

```bash
sudo systemctl start athena-update.service
sudo journalctl -u athena-update.service -n 50 --no-pager
```

Then enable one ATHENA interface:

```bash
# Recommended first: remote text chat through Feishu
sudo systemctl enable --now athena-feishu.service

# Later, to switch to room voice:
sudo systemctl disable --now athena-feishu.service
sudo systemctl enable --now athena-voice.service
```

Do not run both interfaces yet because they share approvals and background state but
are separate processes.

The installer also enables the authenticated dashboard on port 8780. Open
`http://PI_ADDRESS:8780` from a device on the same local network. Its password is
printed once during installation and remains in `/etc/athena/web.env`. Do not expose
port 8780 through router port-forwarding: the local dashboard uses HTTP, not HTTPS.

Enable the two-minute update check:

```bash
sudo systemctl enable --now athena-update.timer
```

Useful status commands:

```bash
systemctl status athena-feishu.service
journalctl -u athena-feishu.service -f
systemctl list-timers athena-update.timer
sudo systemctl start athena-update.service
```

To manually return to an older installed version:

```bash
ls /opt/athena/releases
sudo athena-rollback VERSION --service athena-feishu.service
```

## Using a phone, tablet or laptop as the microphone and speaker

The Pi does not need a microphone or speaker at all. ATHENA keeps running on the
Pi while a device on the same local network supplies both. The control is part of
the dashboard, not a separate page, so there is no second login and no extra port.

### 1. Turn browser audio on

In `/etc/athena/athena.env`:

```text
ATHENA_REMOTE_AUDIO=1
```

Then `sudo systemctl restart athena-voice.service`. The Pi's own ALSA devices stay
closed while this is enabled, so no sound hardware is needed. `Use Browser Audio.cmd`
in this folder does it for you and prints the address to open; `Use Pi Audio
Instead.cmd` switches back.

### Switching back and forth

Both directions are the same one-line setting plus a restart, and both helper
scripts in this folder do it for you — there is nothing to edit by hand:

| ATHENA should use | Run | What it sets |
| --- | --- | --- |
| The device showing the dashboard | `Use Browser Audio.cmd` | `ATHENA_REMOTE_AUDIO=1` — the Pi's own devices stay closed |
| The Pi's own USB speaker and microphone | `Use Pi Audio Instead.cmd` | `ATHENA_REMOTE_AUDIO=0` — the Pi's ALSA devices are used |

Both ask for the Pi's root password. After either one, reload the dashboard page.
Then, under **Command → Microphone and speaker**, press **Start** for browser
audio, or leave it alone for the Pi's own audio.

While browser audio is on, **the Pi's own speaker is silent on purpose**. That is
not a fault and nothing is broken: there is only ever one output, and it is the
connected device. The Detected speech panel on the same card tells you what the
voice service is actually receiving.

To do it by hand instead of with a script: change `ATHENA_REMOTE_AUDIO` in
`/etc/athena/athena.env`, then `sudo systemctl restart athena-voice.service`.

### What runs on the Pi, and what runs on your computer

Nothing is duplicated: each side does the part it is good at.

| Work | Where it happens |
| --- | --- |
| Listening to the microphone (VAD, end-of-speech) | Pi |
| Speech recognition | Cloud |
| Thinking (the language model) | Cloud |
| Speaking a reply out loud | **Your computer**, in its own voice |
| Decoding and playing music | Pi decodes, your computer plays it at 48 kHz stereo |

A reply is only spoken by your computer while the dashboard is attached and the
page has said it can speak; if the browser is closed or cannot, the Pi falls back
to its own voice with no configuration change. Music is sent at the full rate in
stereo precisely because the far end is a real sound card rather than the small
mono device on the board.

### 2. Put the dashboard on HTTPS

A browser only hands a page the microphone in a "secure context", and `http://`
on a local-network address is not one. That is why **Start** fails with
*"Microphone blocked: Cannot read properties of undefined (reading
'getUserMedia')"* — and why music fails the same way, because the speaker
attaches over the same socket. `http://localhost` is a secure context; a LAN
address is not, which is why this only bites when the dashboard is opened from
another device.

Run `Use Dashboard HTTPS.cmd` in this folder. It generates a self-signed
certificate covering the addresses this box answers on, switches the dashboard to
HTTPS, restarts it, and health-checks it.

Then trust that certificate once on every device that opens the dashboard. A
certificate the browser refuses is *not* a secure context either, so the
microphone stays blocked until it is trusted:

- **Windows** — run `Trust Dashboard Certificate.cmd` as administrator, then close
  and reopen the browser (Chrome and Edge read the trust store at start-up).
- **Phone or tablet** — copy it off the Pi with
  `scp root@PI_ADDRESS:/etc/athena/tls/dashboard.crt .` and install it as a
  trusted root certificate.

The certificate is pinned to this box's addresses. If DHCP hands it a new one,
re-run `Use Dashboard HTTPS.cmd` with `--force` and trust the new certificate —
or give the Pi a DHCP reservation and forget about it. Running the same file with
`off` puts the dashboard back on plain HTTP and the microphone goes back to being
blocked.

### 3. Open the dashboard on the device

Open `https://PI_ADDRESS:8780`, sign in, and use **Microphone and speaker** on the
Command tab: press **Start** to hand ATHENA this device's microphone and speaker,
and **Stop** to give them back.

The bar under the buttons is driven by the real captured signal. If it moves while
the room is silent, the wrong input device is selected. Use headphones, otherwise
the microphone hears ATHENA's own voice and the voice detector treats that as
someone speaking.

### Seeing what was heard

The **Detected speech** panel under the buttons is the speech pipeline reporting
on itself. It shows the live partial transcript as words are recognised, the last
completed transcript, whether a turn is open, and — the part that matters when
transcription looks wrong — how much audio was kept. `retained 3.40s in 170
frames` is the audio the voice gate handed to the recogniser for the current
turn, counted from the moment speech started; `dropped 0.00s` is audio the
browser backlog threw away. Retention is lossless as long as `dropped` stays at
zero, which is the normal case: the only bound is the 800 ms pre-roll window,
and it can only ever reach back over silence, never over speech.

Every time the end of speech is detected, ATHENA plays a short falling two-note
blip. It is deliberately the mirror of the rising acknowledgement chirp, so
"I heard you" and "I have stopped listening" are different sounds. Set
`ATHENA_EOS_TONE=0` to turn it off.

Both of these read one small file the voice service writes to `/run` — see
`ATHENA_AUDIO_STATUS_PATH` in `config/athena.env.example`. The same data is on
the console: `python -m athena.audio.telemetry` prints a live line.

### How it works

```text
browser  --websocket-->  dashboard (:8780)  --local socket-->  voice service
```

With HTTPS on, that websocket is `wss://`; the dashboard picks the scheme from the
page, so nothing else changes. Audio never leaves the local network. The dashboard
already refuses non-local addresses and requires its password, so the audio socket
inherits both and no new port is opened. Speech recognition and synthesis still
run in the cloud.

Music uses this same route. Once the dashboard says **Start**, asking ATHENA to
play a song or one of your saved playlists plays through that computer's default
audio output instead of the Pi speaker.

### Notes and limits

- Local network only. Reaching this from outside the house would mean publishing
  the dashboard to the internet, which this project deliberately does not do.
- One device at a time. Starting on a second device takes the audio over.
- Microphone audio is 16 kHz mono and speech is 24 kHz mono, matching the cloud
  speech services. Audio is buffered at most one second; beyond that the oldest
  frames are dropped so replies cannot drift further behind.
- Interruption works: when ATHENA cancels a turn the browser stops queued playback
  immediately instead of finishing the sentence.
- The voice service must be running. If it is not, the dashboard says so instead
  of failing silently.

## Voice hardware

Connect a USB audio adapter or a class-compliant USB microphone and speaker. Confirm
Linux sees them before starting voice mode:

```bash
arecord -l
aplay -l
arecord -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav
aplay /tmp/test.wav
```

If the desired device is not the default, configure ALSA in `/etc/asound.conf`. The
`athena` user is already in the `audio` group.

For a USB composite speaker/microphone whose ALSA card ID is `Device`, install the
included preset with `sudo install -m 0644 config/asound-usb.conf /etc/asound.conf`.
It converts ATHENA's 16 kHz microphone and 24 kHz speech streams to the device's
native 48 kHz formats and sends mono speech to both speaker channels.

## Resource choices for the 4 GB board

- Feishu mode is the lightest and can run continuously.
- Voice mode is also suitable because STT, TTS, and DeepSeek are cloud services.
- Bubblewrap runs generated Python without Docker and without network or host writes.
- Avoid installing Chromium initially. Add it only if JavaScript-only pages are truly
  necessary; ATHENA's `read_webpage` and `search_web` tools remain available.
- Use Ethernet when possible. Wi-Fi and cloud distance affect latency far more than the
  Pi's CPU does.
# Voice optimization release .54

Edge TTS and Qwen STT remain the primary providers. Install the small streaming
wake model once with `python tools/install_keyword_model.py --help` on the
development computer; copy its output directory to
`/opt/athena/models/keyword` on the Pi. Keep `ATHENA_LOCAL_WAKE=1` and
`ATHENA_WAKE_WORD=athena`. Idle room audio stays local; Qwen starts only after
the keyword, or during the existing 20-second follow-up/approval window.
The window restarts after the spoken reply ends. Existing custom prompts remain
unchanged. The compact prompt applies only to the stock voice prompt.

`tools/run_pi_qa.py --tests` runs safe regressions with the Pi's installed
libraries. Live `--prompts` checks make eight bounded billed DeepSeek requests;
the voice QA sends one short Qwen utterance. Do not run paid QA continuously.

