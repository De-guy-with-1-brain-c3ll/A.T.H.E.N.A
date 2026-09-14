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

## 1. Copy this folder to the Pi once

From PowerShell on the computer, replace the address with the Pi's address:

```powershell
scp -r .\orange_pi orangepi@192.168.1.50:/tmp/
```

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
