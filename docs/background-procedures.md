# Background procedures and PC inbox

ATHENA can now queue up to 12 ordered tool steps, retain their progress across
interfaces/restarts, and report completion or failure through the existing alert
callback. Conversation does not wait for the procedure. SQLite claims prevent
voice, Feishu and the dashboard from executing the same job simultaneously.
No new language-model calls are used for execution, progress or recurring checks.

Examples to ask ATHENA:

- “In the background, check my assignments and Shenzhen weather, then report back.”
- “Create a Python calculator in a coding project, write its tests, and test it in the background.”
- “Every 30 minutes, check Shenzhen weather and report the result.”
- “Show the status of my background tasks.”
- “Cancel task TASK_ID.”
- “Export the report for task TASK_ID, then send the report to my computer.”

These are bounded, preplanned procedures—not an unrestricted autonomous agent.
Each step has fixed arguments; later steps do not currently consume earlier
results automatically. Failed steps stop the procedure. A crashed in-flight step
is marked interrupted instead of blindly replaying writes. Cancellation prevents
further steps; the current step may finish. Scheduling currently supports fixed
intervals (at least five minutes), not arbitrary calendar rules. Reports are
retried at most every 30 seconds if the active interface cannot deliver them;
successful recurring jobs schedule their next run after their report is accepted.
Read-only tools and sandboxed coding create/write/check/test are allowed. Shell
commands, downloads, uploads, settings changes and shutdown cannot be smuggled
into an unattended procedure. Those continue to require their normal approvals.

## Receive files on this Windows computer

The new receiver uses the existing aiohttp dependency. From the repository:

```powershell
& .venv\Scripts\python.exe -m pip install psutil paramiko
& .venv\Scripts\python.exe tools\setup_pc_inbox.py --bind YOUR_PC_LAN_IP
```

This starts a hidden receiver, saves a private key in the ignored
`orange_pi/.pc-transfer-key`, and uses `ATHENA Inbox` inside this repository.
Keep the computer awake. Use `--stop` to stop the receiver. To configure the Pi
at the same time, add `--pi PI_LAN_IP`; its SSH password is requested privately.
The helper trusts only SSH host keys already in your known_hosts file.
Restart ATHENA services afterward so they load the new settings.

Windows may require an inbound firewall rule for TCP 8781, scoped to the Pi's IP
and your trusted home network profile. The helper does not change your firewall
or network trust settings automatically.

Uploads are limited to 25 MiB and files in ATHENA's coding/downloads/reports
folders. Hidden files, common credential file types (.env/.pem/.key), databases,
links and junctions are rejected. File contents are not automatically classified;
do not approve a report containing secrets or private school data unless you
intend to transfer it.
Approval binds the file hash and configured destination; changing either requires
fresh approval. Transfers run in the background and provide a verified receipt.
The PC uses unique filenames and never automatically opens or executes uploads.
Requests have HMAC authentication, an integrity hash, an expiry and replay checks.

**Plain HTTP is not encrypted.** Use this mode only on your trusted private LAN
for non-sensitive artifacts. For sensitive files or remote access, put the
receiver behind HTTPS or an encrypted tunnel; do not expose port 8781 publicly.
The PC address must be a private numeric IP. DHCP changes require reconfiguration.
