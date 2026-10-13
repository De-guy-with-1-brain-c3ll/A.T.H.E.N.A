# ATHENA Windows core

ATHENA now has an OpenClaw execution core and a small local controller. DeepSeek
handles conversation and general tasks. Codex handles coding through app-server.
The voice adapter retains ATHENA's audio capture, wake word, recognition, speech
synthesis and interruption pipeline. It does not start the old model loop,
agent supervisor, workflow scheduler or personal-memory consolidation.

The core listens on authenticated loopback port 18790. OpenClaw remains on
authenticated loopback port 18789. Credentials stay in private local files;
console commands and diagnostic output do not include them. No Pi deployment
or remote exposure is part of this installation.

## Console

The independent `athena-console` package uses only Python's standard library.
It is installed in `%USERPROFILE%\.athena\venv`; its Scripts directory is added
to the user PATH. Open a new terminal after installation.

```text
athena
athena ask "What are you working on?"
athena ask "Check task <id>" --session voice
athena task "Fix the failing tests" --cwd C:\path\project
athena task "Inspect this folder" --worker openclaw
athena tasks
athena approve <approval-id>
athena deny <approval-id>
athena status <task-id>
athena report <task-id>
athena attach <task-id>
athena steer <task-id> "Keep the public API unchanged"
athena stop <task-id>
athena resume <task-id>
athena reconcile <task-id>
athena verify <task-id>
athena routines
athena routines add --file routine.json
athena routines update --file routine-update.json
athena routines remove --id <routine-id>
athena improve "Fix this ATHENA bug" --checks checks.json
athena install <improvement-task-id>
athena rollback <improvement-task-id>
athena voice on
athena voice off
athena doctor
athena start
athena stop
athena restart
athena --json tasks
```

Run `athena` once to enter the interactive ASCII console, then type ordinary
messages at `YOU >`. Keyboard buttons `1` through `4` show tasks, approvals,
voice status and help; `0` exits. `/approve ID` and `/deny ID` act directly.
On Windows the console is a live colored dashboard: microphone partial and
final transcripts, listening/thinking/speaking state, active tasks, pending
reviews and streamed conversation text stay visible together. PgUp/PgDn scroll
the retained conversation. Button `3` toggles microphone listening. The local
conversation log retains up to 5,000 operational messages; it is not a profile.
`athena /approve ID` also works from a Windows shell. A bare `/approve` is
an ATHENA console command, not a Windows command.

Separate `athena ask` invocations retain one operational conversation per
working folder, including pending questions and task approvals. The current
folder is passed to the core. `/new` starts a fresh interactive conversation.

Voice uses a separate short-response agent: greetings are at most eight words,
ordinary answers normally stay below twenty-five, and raw task reports remain
in text. Starting voice skips historical notification backlog. Windows now
mirrors the Pi's selected Edge Ava voice through the upstream msedgeservices
transport. It does not substitute a local voice. `athena doctor` shows the
running backend and voice.

Voice and console share task IDs. Console conversation sessions are separate
unless explicitly attached using `--session`. Exiting or detaching from the
console does not stop tasks. Voice listening is off at login; the hidden
`ATHENA Core` startup task launches only the core and Gateway.

## Approvals and evidence

Routine local tasks start automatically: reading, creating files/folders,
reversible edits and bounded coding work. Destructive operations, external
sending/publishing, installations and larger scopes still require review.
Say `yes` or `no` for a single pending action in the current conversation.
Explicit approval starts the chosen worker, without
silently substituting another worker if Codex authentication is unavailable.
Native Codex sandbox approval requests retain their exact IDs and can be
resolved from the console or by saying `approve <id>` to voice.

OpenClaw general workers receive only the scoped ATHENA tool catalog. File
operations stay in the selected workspace. Local Python execution and independent
Python verification require an additional explicit approval because a path check
is not an OS sandbox. Their environment excludes credentials. Use Codex for
arbitrary commands requiring native sandbox and approval handling.

Optional verification checks are supplied by the caller, not invented by the
agent. Pass a JSON file with `athena task --checks checks.json`:

```json
[{"kind":"python","path":"check.py","stdout":"EXPECTED_OUTPUT"}]
```

`reply_received` means an agent replied; `verified` means the supplied independent
checks passed. The check details describe exactly what was verified. Disconnects
and controller restarts mark interrupted work `unconfirmed` and never replay it.
Reconciliation reads worker state before continuation. Task notifications are
queued for the voice pipeline's next safe speaking opportunity.
`athena verify` repeats the recorded independent checks without rerunning the
worker. Executable checks expose a separate approval ID before running.

## Desktop and routines

The dedicated browser supports navigation, status, requested screenshots and
interaction within an approved task. Windows desktop tools expose window
discovery, requested screenshots, terminal launch and targeted input. Images
are interpreted through the configured Qwen vision service because the
conversation model is text-only. Screen capture is request-driven, not continuous.
Browser input focusing and public-link navigation run within the task scope.
Other coordinate activations and Enter require target approval: the controller
cannot infer whether a button sends, publishes or deletes. Typing and focusing
remain within the approved task scope. Codex uses its native workspace sandbox
and on-request approval policy; arbitrary command effects retain that policy's
limitations and are not a universal semantic safety guarantee.
The core defaults to the documented `qwen3-vl-plus` vision model; override using
`ATHENA_PC_VISION_MODEL` / `ATHENA_PC_VISION_BASE_URL` if needed. See
[Alibaba's vision interface](https://www.alibabacloud.com/help/en/model-studio/qwen-vl-compatible-with-openai).

Routines use OpenClaw's validated cron schema; the JSON file passed to `add`
is its native job declaration. Agent task starts still obey approval boundaries.
For file or desktop routines, include `"scope":{"cwd":"C:\\path\\project"}`
and `"sessionTarget":"isolated"`; these use the scoped ATHENA worker.
Recurring routines and operational records are separate from personal profiles.

## Source, latency and updates

`openclaw-fork/source-lock.json` pins the upstream source commit, archive,
dependency lock and build toolchain. `openclaw-fork/build.py` restores, patches,
tests and builds that source. Patches are versioned separately; installed
`node_modules` files are not edited. The original stable runtime remains available.

Persistent WebSocket connections replace per-request CLI startup. The reduced
plugin catalog avoids initializing unrelated providers and channels. The voice
profile bounds context and disables reasoning output, memory flushing and
personal-profile tools. Native Gateway pacing is retained; the voice adapter
adds a maximum 200 ms flush timer once enough speakable text is buffered.

Self-improvement uses an isolated worktree, including the current application
sources without credentials or runtime data. Review the recorded diff and
independent checks before issuing `athena install`. Installation rejects changed
live files, unverified staged changes and deletions, and preserves rollback
copies. It does not automatically restart into an unreviewed change.

For explicit legacy voice startup, set `ATHENA_ENGINE=legacy`. Configuration
backup is `data/openclaw/pre-core-openclaw.json`. Do not restore an old config
while tasks are active. Automatic upstream upgrades are disabled.
