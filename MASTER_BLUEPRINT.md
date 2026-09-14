# ATHENA Master Blueprint

## 1. Mission

ATHENA is an always-available, JARVIS-style personal intelligence. It is not just a chatbot with a voice. It must be able to:

- Hold fast, natural, interruptible conversations.
- Remember useful facts about Benjamin over time.
- Use software and physical tools reliably.
- Understand what is happening across permitted devices and sensors.
- Complete multi-step work while keeping the user informed.
- Act proactively only when the benefit is clear.
- Ask for approval before consequential actions.
- Continue operating as capabilities and hardware are added later.

The first physical node is an Orange Pi Zero 3 with 4 GB RAM. The Orange Pi handles audio, coordination, local tools, and storage. Cloud services handle expensive speech and intelligence workloads.

## 2. First release target

Version 1 is complete when the Orange Pi can:

1. Start automatically after boot.
2. Accept push-to-talk audio.
3. Stream the audio to speech recognition.
4. Send the transcript to DeepSeek V4 Flash.
5. Read Teams assignments through approved Microsoft Graph tools, subject to school authorization.
6. Stream the response into speech synthesis.
7. Begin playing speech as soon as audio arrives.
8. Stop speaking immediately when interrupted.
9. Store conversation and tool history in SQLite.
10. Report measured latency for every turn.

Do not add wake words, cameras, proactive behavior, or unrestricted computer control before this release passes its tests.

First-video scope is voice chat, short conversational context, and read-only Teams assignment access. OneNote, weather, web browsing, coding jobs, long-term memory, scheduling, room control, desktop control, and media are subsequent upgrades. Their design can be prepared now without making them first-release dependencies.

## 3. System architecture

```text
                     CLOUD SERVICES

        Speech-to-text     DeepSeek       Text-to-speech
              ▲               ▲                 │
              │               │                 ▼
══════════════╪═══════════════╪═════════════════╪══════════════
              │          encrypted network      │
              │                                 │
                     ORANGE PI NODE

Microphone -> Audio input -> Turn coordinator -> Audio output -> Speaker
                                   │
                       ┌───────────┼────────────┐
                       ▼           ▼            ▼
                  Tool runner    Memory      Measurements
                       │
              Approved local/network tools
```

The turn coordinator is the center of the application. Providers must not call one another directly. They communicate through the coordinator so one turn can be cancelled cleanly.

## 4. Technology choices

| Purpose | Initial choice | Replaceable later |
|---|---|---|
| Operating system | Minimal 64-bit Armbian/Debian | Yes |
| Language | Python 3.11 | No planned change |
| Concurrency | `asyncio` | No planned change |
| Speech platform | Alibaba Cloud Model Studio, Beijing region | Yes |
| LLM | Official DeepSeek API: `deepseek-v4-flash`, thinking disabled | Yes |
| Speech recognition | `qwen-audio-3.0-asr-flash-streaming` | Yes |
| Speech synthesis | `qwen-audio-3.0-tts-flash` | Yes |
| Data validation | Pydantic | No planned change |
| Database | SQLite through `aiosqlite` | Yes |
| HTTP | `httpx` / provider SDK | Yes |
| WebSockets | `websockets` | Yes |
| Audio | ALSA through `sounddevice` | Yes |
| Logs | Python logging with structured fields | Yes |
| Service management | `systemd` | No planned change |

Every cloud provider must sit behind a small interface. Changing the STT or TTS provider must not require rewriting the agent.

## 5. Repository layout

```text
ATHENA SOURCE/
├── pyproject.toml
├── .env.example
├── .gitignore
├── MASTER_BLUEPRINT.md
├── README.md
├── src/
│   └── athena/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── events.py
│       ├── coordinator.py
│       ├── state.py
│       ├── agent.py
│       ├── prompts.py
│       ├── audio/
│       │   ├── capture.py
│       │   ├── playback.py
│       │   └── devices.py
│       ├── stt/
│       │   ├── base.py
│       │   └── paraformer.py
│       ├── llm/
│       │   ├── base.py
│       │   ├── deepseek.py
│       │   └── speech_chunker.py
│       ├── tts/
│       │   ├── base.py
│       │   └── cosyvoice.py
│       ├── tools/
│       │   ├── models.py
│       │   ├── registry.py
│       │   ├── clock.py
│       │   ├── weather.py
│       │   ├── applications.py
│       │   ├── web_search.py
│       │   └── reminders.py
│       ├── memory/
│       │   ├── database.py
│       │   ├── repository.py
│       │   └── context.py
│       ├── safety/
│       │   ├── permissions.py
│       │   └── confirmations.py
│       └── telemetry/
│           ├── latency.py
│           └── logging.py
├── tests/
│   ├── test_coordinator.py
│   ├── test_tool_registry.py
│   ├── test_speech_chunker.py
│   ├── test_confirmations.py
│   └── test_memory.py
└── deploy/
    ├── athena.service
    └── install.sh
```

## 6. The conversation state machine

The detailed low-latency voice, wake-word, interruption, and output design is in [VOICE_COMMAND_PIPELINE.md](VOICE_COMMAND_PIPELINE.md).

Only one user turn is active at a time.

```text
IDLE
  │ button pressed or wake word
  ▼
LISTENING
  │ user finishes
  ▼
THINKING
  ├── text response ───────────────┐
  └── tool request -> USING_TOOL   │
                         │ result  │
                         └─────────┤
                                   ▼
                                SPEAKING
                                   │ complete
                                   ▼
                                  IDLE

Any state + user interruption -> CANCELLING -> LISTENING
Any state + fatal error         -> RECOVERING  -> IDLE
```

Each turn receives a unique `turn_id`. Every transcript, model token, tool call, and audio packet carries that ID. Data belonging to an old or cancelled turn is discarded.

Required states:

```python
class AgentState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    USING_TOOL = "using_tool"
    SPEAKING = "speaking"
    CANCELLING = "cancelling"
    RECOVERING = "recovering"
```

## 7. Provider contracts

### Speech recognition

```python
class SpeechRecognizer(Protocol):
    async def connect(self) -> None: ...
    async def send_audio(self, pcm: bytes) -> None: ...
    async def results(self) -> AsyncIterator[Transcript]: ...
    async def finish_turn(self) -> None: ...
    async def close(self) -> None: ...
```

`Transcript` contains `text`, `is_final`, `confidence`, and `turn_id`.

### Language model

```python
class LanguageModel(Protocol):
    async def connect(self) -> None: ...
    async def stream_reply(
        self,
        turn: Turn,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> AsyncIterator[ModelEvent]: ...
    async def cancel(self, turn_id: UUID) -> None: ...
    async def close(self) -> None: ...
```

`ModelEvent` is either a text fragment, completed tool request, usage record, or completion event.

### Speech synthesis

```python
class SpeechSynthesizer(Protocol):
    async def connect(self) -> None: ...
    async def send_text(self, turn_id: UUID, text: str) -> None: ...
    async def audio(self) -> AsyncIterator[AudioChunk]: ...
    async def flush(self, turn_id: UUID) -> None: ...
    async def cancel(self, turn_id: UUID) -> None: ...
    async def close(self) -> None: ...
```

These contracts make fake providers easy to use in automated tests.

## 8. DeepSeek configuration

Normal conversation uses:

```python
MODEL = "deepseek-v4-flash"
THINKING = {"type": "disabled"}
MAX_TOKENS = 120
TEMPERATURE = 0.2
STREAM = True
```

Rules:

- Create the API client once during startup.
- Reuse its connections for the life of the process.
- Perform one short warm-up at startup.
- Keep the stable system prompt at the beginning of every request.
- Stream output immediately.
- Limit tool loops to four rounds.
- Set explicit timeouts and no automatic hidden retries.
- Retry a failed safe read operation once; never silently retry a consequential action.

Later, difficult tasks may be routed to V4 Pro. That routing is not part of version 1.

## 9. Turning model text into speech quickly

Do not wait for the complete answer. Accumulate model tokens until a speakable phrase exists.

Flush text to TTS when:

- A sentence ends.
- A comma appears after about 30 characters.
- The buffer reaches about 60 characters.
- No new token arrives for about 120 ms.

Never send individual tokens to TTS. They produce poor rhythm and unnecessary network messages.

On interruption:

1. Stop speaker playback.
2. Empty the playback queue.
3. Cancel the current TTS context.
4. Cancel the DeepSeek stream.
5. Mark the old turn cancelled.
6. Begin the new listening turn.

## 10. Tool system

The model requests tools. The Python hub decides whether and how they execute.

### Tool definition

```python
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    argument_model: type[BaseModel]
    handler: Callable[..., Awaitable[dict]]
    permission: PermissionLevel
    timeout_seconds: float
```

### Permission levels

```text
READ_ONLY       Run automatically: time, weather, search, sensor reading
REVERSIBLE      Run and announce: open app, pause music, change volume
CONSEQUENTIAL   Require confirmation: send, delete, purchase, unlock
FORBIDDEN       Never expose: arbitrary root shell, credential extraction
```

### Tool execution sequence

```text
Model requests tool
       ↓
Is the tool registered? ---- no ----> return unknown_tool
       ↓ yes
Validate exact arguments ---- fail --> return invalid_arguments
       ↓ valid
Check permission
       ├── confirmation needed -> create one pending action
       └── allowed
              ↓
Execute with timeout
              ↓
Return small structured result to model
```

Required rules:

- Pydantic models use `extra="forbid"`.
- Never evaluate model-generated Python inside the hub. Generated programs may run only in the separate isolated coding worker described below.
- Never expose an unrestricted shell tool.
- Log arguments, duration, status, and result summary.
- Hide internal exception details from the model.
- Confirmation authorizes one exact action and expires.
- Webpages, files, and tool output are untrusted data, not instructions.

## 11. Tool roadmap

Implementation update (2026-08-31): `get_weather`, `search_web`, `read_webpage`,
`browse_webpage`, and `coding_workspace` now exist and are registered in the
DeepSeek agent. The coding workspace implements create/list/write/read/check/test/run
for dedicated scratch projects; execution requires Docker and its Python image.
See `src/athena/tools/README.md` for commands, limits, and verification.
The more extensive interfaces below remain the roadmap, not a claim that Teams,
arbitrary project editing, authenticated browsing, or deployment is implemented.

### First video: Teams accessor

- `list_assignments(due_before, status)` returns assigned work and its freshness timestamp.
- `get_assignment(assignment_id)` returns instructions, resources, source links, and the user's submission status where available.
- `get_current_time(timezone)` supports interpreting dates.

Use Microsoft Graph with delegated, read-only access. Complete the school permission check before treating Teams access as available. Links to external assignment websites do not imply access to their contents or completion status.

### Weather

- `get_weather(location, start_date, days)` returns current conditions or forecasts.
- Return location, timezone, units, observation/forecast timestamps, provider, and source link.
- Resolve ambiguous city names before querying; do not infer an exact home address.
- Use a weather API, not model-generated weather or scraping weather pages.
- Cache briefly according to provider policy, and label stale data when offline.
- Later integrate forecasts into briefings and schedule suggestions only with notification permission.
- Select and test a provider for mainland-China connectivity, coverage, pricing, and attribution before implementation.

### Web search and browsing

- `search_web(query)` returns ranked source links and snippets from a search provider.
- `read_webpage(url)` retrieves and extracts public page content.
- `browse_page(url, task)` uses a dedicated browser worker when a page needs JavaScript or permitted interaction.
- Prefer official service APIs, including Graph for Teams/OneNote, over browser automation.
- Start with read-only search, page reading, and source-backed summaries. Authenticated browsing and actions are separate opt-in capabilities.
- Keep source URLs, retrieval times, and content limits. Never present a search snippet as a verified full-page finding.
- Treat web content as untrusted evidence, never as instructions or permission to call tools.
- Prevent public-page fetches from reaching loopback, private networks, metadata endpoints, or non-HTTP schemes; validate redirects and resolved destinations too. Local device tools use a separate explicit allowlist.
- Bound page sizes, redirects, downloads, and processing time. Stop at login, CAPTCHA, paywall, or other access restrictions rather than bypassing them.
- Ask before sending messages, submitting forms with consequences, purchasing, uploading private data, or changing account settings.
- Browser sessions and credentials never enter DeepSeek prompts. Use a dedicated browser profile and isolate it from generated-code jobs.

### Write, implement, run, and test programs

ATHENA should complete a bounded development job, not merely dictate code. The hub coordinates; an isolated worker executes generated programs.

Planned tools:

- `create_coding_job(request, approved_project)` establishes scope and a working copy.
- `read_project_file(job_id, path)` reads allowed project files, excluding secrets.
- `apply_project_patch(job_id, patch)` implements changes within that working copy.
- `run_project_checks(job_id, profile)` runs a preconfigured formatter, test suite, or build.
- `run_program(job_id, profile)` starts an explicitly permitted program inside the worker.
- `get_job_status(job_id)` returns progress, failures, and verified results.
- `cancel_job(job_id)` stops the worker and its child processes.
- `apply_coding_result(job_id)` promotes reviewed changes after approval.

Workflow:

```text
Understand the request and success criteria
  -> inspect approved project
  -> implement in an isolated working copy
  -> add/update tests
  -> run tests/build and bounded runtime checks
  -> inspect failures and repair within the job budget
  -> present diff, artifacts, test evidence, and remaining limitations
  -> obtain approval before applying or deploying outside the working copy
```

Execution requirements:

- Run on a Windows companion or stronger dedicated host, not in the Orange Pi voice process. Start with one coding job at a time.
- Use a properly isolated container or VM with resource limits. A Python virtual environment is dependency isolation, not a security sandbox.
- Mount only the approved working copy; exclude the host filesystem, Microsoft token cache, speech/API keys, SSH keys, browser profiles, and container-control sockets.
- Use an unprivileged account; deny network access by default. Package downloads and other network access require explicit, scoped authorization.
- Enforce CPU, RAM, disk, runtime, output-size, and repair-attempt limits; terminate child processes on cancellation.
- Validate resolved paths and symlinks/junctions so reads and patches cannot escape the workspace.
- Require approval for dependency installation, persistent services, opening network ports, deployment, modifying the live ATHENA service, and destructive operations outside disposable job files.
- Preserve user changes and retain a diff and rollback copy. Never allow a coding job to silently rewrite the running hub or its permission policy.
- Run existing tests as well as new tests. Check at least one representative program behavior; a successful syntax check is not proof the program works.
- Report actual commands/checks, exit codes, and relevant output. State clearly when tests could not run or coverage is incomplete.

Coding jobs and multi-page browsing are background jobs with independent budgets, not extensions of the short four-round voice tool loop. A quick acknowledgement is not task completion. Let the user continue talking and send concise progress or completion updates under the notification policy.

### Other planned capabilities

- OneNote reading/search, personal and task memory.
- Calendar planning, reminders, active checks and notifications.
- Lights, room sensors, physical controls, microphone and speaker upgrades.
- Allowlisted desktop actions, music and video control.

## 12. Memory design

Use three layers:

### Working context

The most recent 8–12 conversation messages. Sent to the model.

### Rolling summary

A compact description of older conversation. Updated outside the critical speech path.

### Durable facts

Explicit facts such as name, preferences, devices, routines, and relationships. Each fact has a source, timestamp, and confidence.

Do not automatically store passwords, API keys, financial details, private messages, or every casual statement.

Initial SQLite tables:

```sql
CREATE TABLE turns (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    user_text TEXT,
    assistant_text TEXT,
    status TEXT NOT NULL,
    first_audio_ms REAL,
    total_ms REAL
);

CREATE TABLE tool_runs (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result_json TEXT,
    status TEXT NOT NULL,
    duration_ms REAL
);

CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_turn_id TEXT,
    updated_at TEXT NOT NULL
);
```

Database writes happen after first audio begins, not in the latency-critical path.

## 13. Latency budget

Measure these timestamps:

```text
speech_started
speech_ended
transcript_final
llm_request_started
llm_first_text
tts_text_sent
tts_first_audio
playback_started
turn_completed
```

Primary calculation:

```text
speech-to-first-audio = playback_started - speech_ended
```

Version 1 targets:

| Metric | Median target | P90 target |
|---|---:|---:|
| Final transcript after speech | 300 ms | 500 ms |
| DeepSeek first text, warm | 550 ms | 750 ms |
| TTS first audio | 200 ms | 350 ms |
| Total speech-to-first-audio | 1,000 ms | 1,400 ms |
| Stop audio after interruption | 150 ms | 250 ms |

The current desktop DeepSeek measurement, approximately 491 ms median, satisfies the model-stage target.

Optimizations:

- Keep all network connections open.
- Use Ethernet.
- Use nearby provider regions.
- Disable thinking for ordinary speech.
- Begin STT while the user is speaking.
- Begin TTS on the first speakable phrase.
- Use a 40–80 ms playback buffer.
- Keep prompts and tool results compact.
- Move database work and summaries outside the response path.
- Record p50, p90, and p99 rather than relying on one run.

## 14. Configuration and secrets

Use environment variables, never Python constants:

```text
DEEPSEEK_API_KEY=
DASHSCOPE_API_KEY=
DASHSCOPE_WORKSPACE_ID=
ATHENA_TIMEZONE=Asia/Shanghai
ATHENA_DATABASE_PATH=/var/lib/athena/athena.db
ATHENA_LOG_LEVEL=INFO
```

On the Orange Pi, store them in:

```text
/home/orangepi/.config/athena.env
```

Protect the file with mode `600`. Never copy the Windows virtual environment or secret files to Git.

## 15. Orange Pi deployment

Use minimal 64-bit Linux without a desktop. Install Python, Git, audio libraries, and SSH. Create a new Linux virtual environment on the Pi because the Windows environment is incompatible with ARM Linux.

The production service runs as an unprivileged user:

```ini
[Unit]
Description=ATHENA Personal Agent
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=orangepi
WorkingDirectory=/home/orangepi/athena
EnvironmentFile=/home/orangepi/.config/athena.env
ExecStart=/home/orangepi/athena/.venv/bin/python -m athena.main
Restart=on-failure
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Development workflow:

```text
Windows VS Code -> private Git repository -> Orange Pi git pull -> restart service
```

Use VS Code Remote SSH for microphone and hardware debugging directly on the Orange Pi.

## 16. Build plan

### Phase 0 — Repository foundation

- Add `pyproject.toml` and package layout.
- Add configuration validation.
- Add structured logging.
- Move the latency tester under `tools/` later.
- Add automated tests.

Exit test: `python -m athena.main` starts and shuts down cleanly.

### Phase 1 — Text agent

- Implement the DeepSeek adapter.
- Implement streamed terminal input/output.
- Add the state machine.
- Add time and read-only Teams tools; verify Microsoft authorization first.
- Add validation, limits, timeouts, and tool logs.

Exit test: 100 scripted requests complete without a malformed tool being executed.

### Phase 2 — Speech output

- Implement the speech chunker.
- Implement the TTS adapter.
- Implement low-buffer PCM playback.
- Implement cancellation.

Exit test: streamed text begins playing before model completion and stops within 250 ms of cancellation.

### Phase 3 — Push-to-talk speech input

- Detect microphone and speaker devices.
- Capture PCM without writing temporary audio files.
- Implement streaming STT.
- Connect GPIO button or keyboard input.
- Add end-to-end latency measurements.

Exit test: 90% of 50 common commands are transcribed correctly and median first audio is near one second.

### Phase 4 — Natural turn taking

- Add local voice activity detection.
- Add interruption handling.
- Add echo cancellation or a suitable USB speakerphone.
- Add wake-word detection.

Exit test: ATHENA can be interrupted reliably without repeatedly interrupting itself.

### Phase 5 — Memory and reminders

- Add SQLite migrations.
- Add recent conversation context.
- Add rolling summaries.
- Add explicit durable memory operations.
- Add scheduled reminders.

Exit test: remembered facts can be inspected, corrected, and deleted.

### Phase 6 — JARVIS expansion

- Computer-control node.
- Home automation bridge.
- Sensors and room awareness.
- Camera/vision adapter.
- Background task manager.
- Proactive notification rules.
- Additional Orange Pi room nodes.
- Weather API integration and source-backed web search/reading.
- Dedicated browser worker for permitted interactive pages.
- Isolated coding worker for implementation, program execution, testing, and reviewed changes.

Each new capability must use the same tool permissions and turn IDs.

## 17. Test strategy

### Unit tests

- Speech chunk boundaries.
- State transitions.
- Tool argument validation.
- Permission classification.
- Confirmation expiry.
- Context length limits.
- Latency calculations.

### Integration tests

- Fake STT -> fake LLM -> fake TTS complete turn.
- LLM requests a valid tool.
- LLM requests an invalid tool.
- Tool times out.
- User interrupts during LLM generation.
- User interrupts during TTS playback.
- Provider disconnects and reconnects.
- Weather results include correct location, units, timestamps, and offline/stale behavior.
- Web content cannot authorize tools or reveal credentials; private-network fetches and unsafe redirects are rejected.
- Coding jobs cannot escape the project, read credentials, access the network without authorization, or exhaust host resources.
- Cancelling a coding job terminates child processes; failed tests never become reported successes.
- Existing project edits survive working-copy creation and reviewed promotion.

### Hardware tests

- Microphone capture for one hour.
- Simultaneous capture and playback.
- Ethernet disconnect and recovery.
- Reboot and automatic service start.
- CPU temperature under continuous use.
- 24-hour memory stability test.

## 18. Non-negotiable rules

1. Never put a secret in source code.
2. Never give the model unrestricted shell access.
3. Never report tool success before receiving success.
4. Never execute consequential actions without confirmation.
5. Every asynchronous item has a `turn_id`.
6. Every external call has a timeout.
7. Every provider is replaceable.
8. Every important action is logged.
9. The voice path takes priority over memory and background work.
10. Build and test one phase before adding the next.

## 19. First coding session

Create these files first, in this order:

1. `pyproject.toml`
2. `src/athena/config.py`
3. `src/athena/events.py`
4. `src/athena/state.py`
5. `src/athena/llm/base.py`
6. `src/athena/llm/deepseek.py`
7. `src/athena/tools/models.py`
8. `src/athena/tools/registry.py`
9. `src/athena/tools/clock.py`
10. `src/athena/coordinator.py`
11. `src/athena/main.py`
12. Corresponding tests

The first executable milestone is text-only:

```text
Benjamin types -> coordinator -> DeepSeek -> optional safe tool -> streamed text
```

Once that foundation is reliable, audio can be connected without changing the agent or tool architecture.
