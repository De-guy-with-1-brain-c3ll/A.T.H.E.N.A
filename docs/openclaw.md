# OpenClaw for ATHENA

OpenClaw runs on the Windows PC using its own Node runtime. Its packages,
configuration, credentials references, sessions and workspace live in the ignored
`data/openclaw` folder. ATHENA's voice, memory, dashboard and existing tools remain
in place. OpenClaw's model calls use the existing `DEEPSEEK_API_KEY` from `.env`;
that credential is referenced by name rather than copied into its model config.
Model usage is billed by the provider as usual.

This installation uses OpenClaw **2026.8.1** and Node **26.11.1**. Version
2026.9.9 failed during initial database setup on this computer; its separate
installation is retained in `data/openclaw/runtime` for diagnosis. The launcher
uses the working installation in `data/openclaw/runtime-stable`. Check the
Windows initialization issue before upgrading; avoid a blind automatic update.

From the ATHENA project folder in PowerShell:

```powershell
.\tools\start_openclaw.ps1
.\tools\openclaw.ps1 health
.\tools\openclaw.ps1 dashboard
```

For the terminal chat, use `./tools/openclaw.ps1 tui`. For a direct task:

```powershell
.\tools\openclaw.ps1 agent --agent main --session-key athena-manual --message "Inspect the ATHENA project and report its current structure. Do not modify files."
```

After restarting ATHENA, its model can select `openclaw_task` for a requested PC
task. When ATHENA runs on the Pi, it delegates through the existing signed PC
receiver (`/openclaw`) to the Windows installation. Restart the PC receiver and
ATHENA after installing this update. The PC receiver must be running and the
existing PC transfer URL and key configured on the Pi. OpenClaw's gateway remains
bound to localhost; its token is never sent to the Pi.

`pc_desktop` opens an empty, visible PowerShell terminal in the Windows Desktop
folder through the same bridge (`/desktop`), without delegating a trivial launch
to an agent. This action accepts no commands. A successful receipt includes the
new process ID and working directory; it confirms launch, not visual focus.

For a Windows app using a separate data directory, set `ATHENA_OPENCLAW_DIR` to
the absolute path of this project's `data/openclaw` installation.

ATHENA uses `openclaw_task` as its coding tool when OpenClaw is configured.
Code creation, debugging, changes and tests are delegated with the full objective.
The existing confirmation flow asks before starting work because the agent can
modify files and execute commands. Approval submits a background task and returns
its ID immediately; it does not claim the requested code is complete.

Ask "What is the status of OpenClaw task <id>?", "How is the coding task going?"
or "Give me the final report on OpenClaw task <id>". The read-only
`openclaw_status` tool queries the PC worker. It reports queued/running state,
actual recent tool calls/results, or the final agent report. Status/report
requests never restart work and need no new approval. Task receipts survive a
worker restart, but interrupted tasks are marked unconfirmed and never retried
automatically. Agent replies use `reply_received`, not independently verified
completion. There is no invented percentage or automatic spoken notification.

Recorded tool names and states are read from the pinned 2026.8.1 runtime's local
session database; commands, arguments, output and unrelated sessions are not
exposed. If a runtime upgrade changes that schema, detailed progress becomes
unavailable rather than guessed. ATHENA's personal-memory database is not
synchronised with OpenClaw. The dashboard's generic submission receipt remains
submitted; use the live OpenClaw status tool for the PC worker's actual state.

The gateway passed a real task that read ATHENA's README, executed
`node --version`, wrote `setup-verification.txt` in its workspace and read it
back. The saved content was independently verified outside the agent.

If a request times out or the connection drops, inspect the session in OpenClaw
before retrying. Losing the caller does not prove the underlying work stopped.
An agent reply is not proof that every requested action succeeded; inspect its
tool results and generated files.

The gateway listens only on this PC, with token authentication. No messaging
channels or microphone are activated by this setup. The default heartbeat and
automation scheduler are disabled; enable scheduling when configuring your first
recurring task. The hidden
launcher starts it for this Windows session; it is not registered for login startup.

For ATHENA code updates, the workspace instructions require a separate Git
worktree, relevant tests and a reviewable diff. Installation/restart is a separate
step with rollback, preserving the running assistant and any uncommitted work.

Gateway logs are in `data/openclaw/gateway.stdout.log` and `gateway.stderr.log`.
To reconfigure the existing installation, run
`.venv/Scripts/python.exe tools/configure_openclaw.py`.
Run `tools/install_pc_agent_pi.cmd` to install the signed update on the Pi using
its existing rollback-capable updater. Enter its SSH password locally when asked.
For a real coding/submission/status/report check on Windows, run
`.venv/Scripts/python.exe tools/check_openclaw_coding.py`; this creates and tests
one synthetic Python file in the OpenClaw workspace and independently checks it.

Official references: [installation](https://docs.openclaw.ai/install),
[external app integration](https://docs.openclaw.ai/gateway/external-apps), and
[source repository](https://github.com/openclaw/openclaw).
