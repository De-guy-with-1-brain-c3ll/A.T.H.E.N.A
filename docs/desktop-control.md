# ATHENA Control

Run `ATHENA Control.exe` on your desktop. Enter the Pi IP and **dashboard password**, not the SSH/root password. A blank password works only if dashboard authentication is disabled. Connect and approve the Pi certificate once on your trusted network; the app remembers the certificate, never your password. A changed certificate requires explicit pairing again.

Use **Find Pi** to scan the displayed private LAN subnet if its address changes. Scans are limited to /24, never the public internet. A remembered certificate distinguishes your Pi from other discovered controllers. Adjust the subnet if Windows picked a VPN adapter.

Tabs cover live tool/download/transfer progress, subagents, all runtime settings, prompts, text chat and recent conversations, and measured usage/delay. Monitoring is local and never invokes an AI model. Text chat uses the same Pi memory store as the dashboard and voice.

- Download percentages are received bytes. Transfer percentages are bytes submitted to the network; only a verified PC receipt confirms completion.
- Tools without measurable stages show execution state, not a fictional percentage. Old transfer samples are marked unconfirmed.
- DeepSeek rough estimates and provider-reported counts are separate. Qwen STT displays measured audio seconds because translating them into text tokens would be misleading. Qwen fallback TTS displays submitted characters and a rough text-token estimate, not audio-token billing. Edge speech/cache replay adds no Qwen usage.
- Usage covers the last 3000 recorded conversation/agent and speech events since this release, across interfaces, not historical account billing. Separate memory-consolidation and vision requests are not covered by these counters.
- Dashboard round-trip delay includes server handling and is not speech latency. Voice records correlate the captured utterance with its background reply: endpoint-to-first-text and endpoint-to-audio-ready, plus response-delivery stages. The endpoint includes silence detection; audio-ready is not the physical speaker start. Browser-native voice isn't measured by this counter.
- Voice on/off/restart is separate from restarting all ATHENA services and rebooting the Pi. Reboot requires typing `REBOOT PI`; monitoring/tests never reboot or play sound.
- Computer microphone streaming still requires the browser audio page to remain open. This native frontend replaces ordinary dashboard controls, not its microphone transport.

Build from the repository: `.venv/Scripts/python.exe -m PyInstaller --noconfirm --onefile --windowed --paths src --name "ATHENA Control" tools/athena_control.py`. Run with `--self-test` for a silent GUI startup check.
