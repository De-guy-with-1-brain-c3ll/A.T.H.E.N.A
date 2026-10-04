# Supervised background agents

ATHENA can delegate an objective with `agent_task` (`spawn`, `status`, `report`,
`steer`, `cancel`). Spawning only creates a durable queue entry: conversation
returns immediately and the supervisor launches work separately. Dashboard,
voice, terminal and Feishu share `ATHENA_DATA_DIR/agents.sqlite3`.

The dashboard **Agents** page shows live progress and reports, refreshed every
three seconds while visible, with a subtree cancellation button.

Workers plan, call read-only tools or sandboxed coding create/write/check/test,
inspect results and refine their work. They can delegate narrower objectives to
children, check progress and consolidate reports. A parent waiting for children
releases its worker slot and resumes only after they stop; idle checks never call
DeepSeek. Reports must cite real successful operation IDs. A promise, missing
evidence or malformed report cannot become a successful task.

Limits: two active workers globally across processes, twelve pending agents,
three children per parent, two delegation levels and three phases per agent.
Default root deadline is five minutes (up to ten); its default conservative
token reservation ceiling is 60k (up to 100k). Root and children share this
ceiling and at most eighteen requests. Reservations use a high UTF-8-byte input
bound (including Chinese text and code) plus maximum output;
they are safety estimates, not provider-billed token counts. Per-phase model
loops stop after eight rounds. Concurrency cannot multiply the root ceiling.

Cancelled or interrupted workers do not replay writes. Cancelling a parent also
cancels its children. Guidance arrives at the next model step, not halfway
through a tool. Downloads, uploads, shell commands, device control and settings
are NOT delegated; they retain the foreground's approval path. Child capabilities
can only narrow. Read-only results, web content and program output are untrusted.

Examples: “Delegate researching these three topics and give me one report”,
“Create and test the Python program in a background agent”, “Check my agents”,
“Give agent ID new guidance: focus on Shenzhen”, “Cancel agent ID”.

No worker processes or paid model requests are started while the queue is empty.
Automated regression tests use fake models and audio devices; no speaker test is
needed. Live provider behavior and school API permissions are separate from the
offline regression suite.
