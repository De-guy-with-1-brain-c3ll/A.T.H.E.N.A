# ATHENA tools

Weather, web search, webpage reading, JavaScript rendering, and a coding workspace
are implemented and discovered by the existing DeepSeek voice-agent tool loop.

## Try them

From the project folder in PowerShell, start a text conversation (no microphone or
Qwen speech credentials required; uses DEEPSEEK_API_KEY from your existing .env):

```powershell
.\.venv\Scripts\python.exe -m athena.tools chat
```

Try: "What's the weather at 31.23,121.47?", "Search the web for Python's official
documentation and read the first official result", or "Create a calculator in
project calculator, save its Python files, write unit tests, and test it."

The same tools are available when restarting the voice app:

```powershell
.\.venv\Scripts\python.exe -m athena.main
```

## Voice speed, shutdown, and approved downloads

- Restart ATHENA using the existing desktop shortcut to load these changes.
- Voice speed defaults to 1.2×. Ask to change `tts_speech_rate` to another value
  from 0.5 to 2.0; the new value takes effect on the next speech session, without
  restarting the app. This uses Qwen3's speech-rate parameter, not a pitch change.
- Say **"ATHENA, shut down"**. It says goodbye, exits the voice loop and releases
  audio resources. It does not shut down Windows, stop Docker, or delete anything.
  The desktop shortcut's terminal may remain open because it uses `-NoExit`.
- Ask for a file download. ATHENA first inspects headers and resolves public HTTPS
  redirects (no file body is downloaded). The hub then names the actual filename,
  destination site, and inspected size; the terminal shows the exact URL.
  Say **"yes"**, **"I approve"**, **"approve download"**, or **"no"** to cancel.
  These responses authorize only a real pending request, never a model-written
  question. Approval expires after two minutes and is consumed once. A different
  task cancels pending approval; asking to see the inspected URL preserves it.
  Short confirmations use the voice-activation threshold only while approval is
  pending; arbitrary short noises and mixed-language lookalikes aren't approvals.
- Files are saved under `data/downloads`, with a 1 GiB / 10-minute transfer limit.
  Only public HTTPS URLs are supported. CDN redirects are resolved before consent;
  if the destination changes again during transfer, it must be prepared and approved
  again. HTML responses are rejected instead of saving a webpage as an installer.
  An imager application and an operating-system image are different artifacts.
  Existing files are never overwritten; partial
  files are cleaned up on errors/cancellation. Nothing is automatically opened,
  extracted or run, and Windows internet-file warnings are retained.
- Download-status questions are answered directly by the hub, without asking the
  model to infer state from conversation memory. The tracked states are preparing,
  waiting for approval, downloading, completed, failed, cancelled, and missing.
  Asking for status does not consume or cancel an outstanding approval. A completed
  status is cross-checked against the saved file. Transfers report completion only
  after the final file is atomically published; cancellation removes partial files.
- English recognition uses `fun-asr-realtime` with `language_hints=["en"]` at 16 kHz.
  The previous `fun-asr-flash-8k-realtime` choice supports Chinese only according to
  [Alibaba's SDK documentation](https://www.alibabacloud.com/help/en/model-studio/fun-asr-realtime-python-sdk).
  Recognition of names and accents still needs testing with the actual microphone.
- Tool planning/narration and reasoning fields are not spoken. DeepSeek's slow
  thinking mode remains disabled. See the background voice behavior below.

## Background voice agents and quick acknowledgement

The voice app now runs each model/tool request in an independent background job
(up to three outstanding requests). You can ask another question while a search,
code/test job, approved download, or model response is still running. Each worker
has its own conversation snapshot and approval state, while sharing the existing
DeepSeek HTTP connection pool. No additional model provider or library is needed.

At startup, ATHENA synthesizes **"On it."** once through the configured Qwen voice
and keeps that short PCM clip in RAM. This is one small speech-service request per
launch, using your existing account; it is not assumed to be free. Playback needs
no network request. If preparation fails, a short local tone replaces the clip.
The cached clip uses the speech speed selected at startup; normal replies still
use live speed settings. Restart to refresh the acknowledgement voice/speed.

When DeepSeek opens its response stream (response headers received), the hub queues
that acknowledgement immediately. If opening the stream takes over one second,
the same acknowledgement is queued at that point instead: it acknowledges receipt,
not success or a confirmed connection. It is played at most once per job. An already
completed answer takes priority, so fast answers can skip the acknowledgement.

Acknowledgements and results wait while voice activity is detected, including
the first few frames before full activation. The hub waits for the final transcript
rather than interrupting a pause mid-sentence. Listening pauses during ATHENA's own
speech to avoid microphone echo, then resumes. **This is not full-duplex barge-in:**
you can talk while work runs, but not reliably interrupt spoken output yet.

Real answers remain buffered per model round to suppress tool-planning text and
fake download-approval questions. The acknowledgement is fast; it does not make
the actual model answer, tool operation, or Qwen synthesis instantaneous. Real
microphone/network latency must still be measured on the target machine.

- Say **"background status"** to hear the number of outstanding requests.
- Say **"cancel background tasks"** to cancel all outstanding jobs and pending
  download consent. Cancellation does not undo already completed actions/files.
- Say **"ATHENA, shut down"** to cancel workers and stop the assistant.
- Background download inspection cannot authorize a transfer. Only the exact
  request whose approval prompt was delivered accepts your next confirmation.
  Concurrent approval prompts are queued, not allowed to overwrite one another.
  The two-minute consent window starts when the prompt is delivered.
- Answers are printed with the original request above them. Completed results
  enter memory only after delivery; pending requests are marked as unfinished
  in the context supplied to subsequent workers.

The text-only `athena.tools chat` command retains its sequential interaction; the
concurrent listening and cached acknowledgement are features of the voice hub.

## Texting ATHENA from a terminal

Run `athena-chat` for a persistent text conversation with no microphone, speaker,
STT, TTS, or DashScope connection. It uses the same DeepSeek model, persistent
memory database, live settings, discovered tools, download-consent rules, and
ATHENA system prompt as voice mode. Type `/help`, `/exit`, or a normal request.

Send a single message and exit with `athena-chat "your request"`. If the command is
not yet available after pulling these files, run `.\.venv\Scripts\python.exe -m
pip install -e .` once, or use `.\.venv\Scripts\python.exe -m athena.text` directly.
Voice mode and text mode should not be run simultaneously against the same memory
database while both are actively writing; use one interface at a time for now.

If DeepSeek has a transient connection failure, text chat retries one fast failure
once. A timeout is not doubled. If the retry fails, ATHENA prints a short actionable
message and returns to `You:` instead of displaying a traceback or closing the chat.
Provider failures are not written into long-term conversation memory.

## Texting ATHENA remotely through Feishu

ATHENA includes a private Feishu (飞书) interface using Feishu's outgoing
long-connection mode. It needs no public IP, inbound firewall rule, port forwarding,
or tunnel. It shares the same DeepSeek client, memory database, tools, task limits,
download state, command state, and approval rules as local text chat. Run only one
ATHENA interface at a time until shared multi-process database coordination is added.

1. In the Feishu developer console, create an enterprise self-built app and enable
   its bot capability.
2. Grant the bot permission to receive private messages and send messages as the app.
3. Under Events and Callbacks, select long connection, then subscribe to
   `im.message.receive_v1` (Receive message v2.0).
4. Create and publish an app version, then add/open the bot in Feishu.
5. Add `FEISHU_APP_ID` and `FEISHU_APP_SECRET` to the project `.env`. Never paste
   either value into a Feishu message. `FEISHU_ALLOWED_OPEN_IDS` is optional.
6. Install/update the project and launch the connector:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\athena-feishu.exe
```

On first launch, the terminal prints a random six-digit pairing code. Send
`/pair 123456` (using the displayed value) to the bot in a private Feishu chat.
Only the paired Feishu `open_id` is persisted in `data/feishu_allowed.json`; the
pairing code is one-use and is not stored. Group messages, bot messages, duplicate
events, non-text content, and unpaired senders cannot invoke ATHENA.

Every accepted message receives a task ID immediately. Slow model, web, coding and
tool work continues in the existing background queue; its result is sent later.
Use `/tasks` or `/cancel ID`. Command/download approvals stay tied to the Feishu
account that received the exact approval prompt and expire normally. The event
callback only queues work, allowing Feishu's required fast acknowledgement.

Only a download prompt accompanied by a separate terminal line beginning
`Download approval:` is a real hub-created grant. Model-written approval wording is
discarded regardless of the filename or sentence length, and the model gets one
chance to issue the required `download_file` tool call. Inline pre-approval such as
"download it, I approve" cannot skip inspection; ATHENA must stage the exact file
and ask again. Text-chat build v0.3 includes this boundary.

Text-chat build v0.4 also audits website reachability claims. A search result is not
a connectivity test. Before saying GitHub cannot be reached, the model must make a
fresh `read_webpage` or `browse_webpage` call to an actual GitHub URL. Claims that
contradict a successful direct call are suppressed. Questions such as "is web
browsing working?" run Chromium against a known page and report its real result.

Official GitHub release files can be resolved with `find_github_release_asset`.
It reads the latest release page and GitHub's official lazy-loaded asset fragment,
selects one exact asset, and returns its stable GitHub URL without downloading it.
The Armbian Imager request is additionally host-routed: "download the Armbian
Imager" resolves the latest Windows x64 setup asset and passes it into the normal
HEAD inspection and approval gate without relying on DeepSeek's reachability claim.
The less specific "Armbian download" asks whether the user means the Imager or an
operating-system image. Signature assets such as `.exe.sig` cannot be mistaken for
the actual installer.

## Token efficiency

Text-chat build v0.5 selects tool schemas locally before calling DeepSeek. Ordinary
conversation sends no tool schemas; weather and time each send one; coding sends
the coding tool; and downloads send only web/release/download tools. In the current
schema set this reduces repeated tool-definition text from 5,789 characters to 0
for chat, 596 for weather, 324 for time, 1,128 for coding, and 2,949 for a general
download. Direct host commands, including approved-download controls and the
Armbian resolver, use no DeepSeek answer tokens at all.

Only eight recent turns (previously twelve) and fifteen highest-priority facts are
included in each prompt. Memory still saves every turn locally, but DeepSeek memory
consolidation runs once per six completed turns instead of after almost every turn.
The memory summary output cap is 600 tokens instead of 1,000. This preserves local
history while reducing extra memory-model calls by roughly five out of six.

In text mode, `/usage` prints the session's approximate answer-request input/output
tokens and the number of memory-summary calls. It is a local character-based
estimate; the official DeepSeek billing dashboard remains authoritative.

Each turn also has a cumulative estimated-input ceiling: about 12k tokens for chat,
24k for ordinary tool use, 30k for downloads, and 80k for multi-file coding. Tool
rounds are capped at 3, 5, 6, and 12 respectively. When a ceiling is reached ATHENA
stops and asks for a narrower task instead of silently continuing a runaway loop.

List tools with `python -m athena.tools list`. Direct calls accept a JSON file:
`python -m athena.tools call get_weather --arguments-file weather.json`, containing
`{"location":"31.23,121.47","days":3}`.

## What is supported

| Tool | Behavior |
| --- | --- |
| get_weather | Current conditions, hourly and 1–7 day forecasts; ten-minute cache; asks about ambiguous cities. |
| search_web | Bing RSS results with source links and snippets. No extra key. This is not a contracted search API: blocking or format changes can break it. |
| read_webpage | Fast public HTML/text extraction with source links and retrieval time. |
| browse_webpage | Headless Chromium for JavaScript pages; read-only, no login, forms, media or downloads. |
| coding_workspace | Creates projects, writes/reads files, checks syntax, runs Python and unittest tests, and returns real output/errors for the model to fix. |

Generated source files persist under `data/coding/<project>/`. This tool does not
edit ATHENA itself or arbitrary existing projects. Python execution currently
supports the standard library only; JavaScript and other files can be written but
not executed. Programs run in a fresh disposable container; runtime-generated
files are discarded. No tests discovered counts as failure, not success.

Open-Meteo's free endpoint is for non-commercial use; check its
[terms/pricing](https://open-meteo.com/en/pricing) before a commercial deployment.
Weather results include attribution. No provider or website is guaranteed
reachable from every mainland China network. Network errors are reported, never
replaced with invented results. DeepSeek and speech service billing is unchanged.

## Execution dependency: Docker

Writing and reading code works without Docker. Running/checking/testing generated
code requires Docker with Linux containers, started, and this pre-downloaded image:

```powershell
docker pull python:3.12-slim
```

Docker Desktop 4.88.1 and WSL 2.7.12.0 are installed on this Windows PC.
On 2026-08-31, the initial per-user Docker installation failed to start with a
missing-registry-key error. A clean all-users reinstall to
`C:\Program Files\Docker\Docker` resolved it. The Linux engine is running and
`python:3.12-slim` was pulled successfully. Real isolated execution tests passed
(successful tests, failing tests, no tests discovered, and infinite-loop timeout).
Keep Docker Desktop running when using execution. Reopen existing terminals/apps
if they still have the old installation PATH. On another machine, install/start
Docker Desktop on Windows, or Docker Engine on Linux. The tool
deliberately has no fallback that executes AI-written programs on your PC.
Containers have no network, no host mounts, no API keys, a read-only base filesystem,
an unprivileged user, memory/process/CPU limits, and at most a 30-second run.
Keep Docker and its image updated; containers reduce risk but aren't a perfect
security boundary for a hostile public multi-user service.

Web requests reject private/local destinations, including redirects and DNS
answers. Browser requests pass through the same checks. External page text and
program output are marked untrusted; they are not user instructions. These tools
do not implement authenticated Teams access or desktop control.

## Setup on another machine and verification

```powershell
python -m pip install -e .
python -m playwright install chromium
python -m unittest discover -s tests -p "test_*.py" -v
```

On supported Linux systems Chromium may also need operating-system libraries
(`python -m playwright install --with-deps chromium`). On an SBC, browser rendering
and Docker use more RAM than the fast HTML reader; test the target OS/architecture.

Optional integration tests, separately enabled:

```powershell
$env:ATHENA_TEST_BROWSER = '1'
python -m unittest discover -s tests -p test_tools_browser.py -v
$env:ATHENA_TEST_NETWORK = '1'
python -m unittest discover -s tests -p test_live_tools.py -v
$env:ATHENA_TEST_DOCKER = '1'
python -m unittest discover -s tests -p test_coding_tools.py -v
```

Regular unit tests mock external services and execution; the Docker test actually
runs passing, failing, empty-test, and timeout cases when explicitly enabled.
`ATHENA_TEST_DEEPSEEK=1` separately enables a small billed model/tool round-trip in
`test_live_tools.py`, using the configured key without printing it.

Implementation verification on this PC: unit tests, real JavaScript rendering,
and live weather/search/HTML/browser checks passed. Following the clean Docker
reinstall, the coding suite passed seven tests, including real container execution;
one symlink test was skipped because this Windows account could not create symlinks.
A live model test was previously skipped because
`DEEPSEEK_API_KEY` was not configured in the process/project environment; the
streamed model/tool loop was tested with a mock model instead.

## Adding more tools

1. Copy `_template.py` into this directory.
2. Rename it without a leading underscore, for example `lights.py`.
3. Give the tool a unique `definition.name`.
4. Write a precise description and JSON input schema.
5. Choose `SAFE`, `CONFIRM`, or `DANGEROUS` permission.
6. Implement `execute()` and export `create_tools()` returning fresh tool instances
   (or a legacy `TOOL = YourTool()` singleton).
7. Restart ATHENA. `ToolRegistry.discover()` loads it automatically.

Tool files must never contain credentials. Read service credentials from the
environment or `.env`. Put one primary tool in each file. Return a short
`spoken_text` plus structured `data` for logging or future model follow-up.
