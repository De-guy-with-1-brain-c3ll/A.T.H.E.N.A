# ATHENA's pinned OpenClaw source runtime

`source-lock.json` pins OpenClaw `v2026.8.1`, commit
`ea806575e6450e4d1efdfc72c19f04be982a1b9b`, the source archive checksum,
dependency lock checksum, Node `26.11.1`, pnpm `12.1.0`, and ordered local patches.
Automatic upstream upgrades are disabled. The ignored source checkout lives in
`data/openclaw/source`; the original installed runtime remains in
`data/openclaw/runtime-stable` for rollback.

## Rebuild on Windows

From ATHENA's project directory:

```powershell
.venv\Scripts\python.exe openclaw-fork\build.py --restore --install --test
```

The helper restores source only when absent, verifies the pinned archive and
dependency lock, applies or verifies the patch series, installs pinned build
tools, runs the focused source tests, and builds through OpenClaw's normal
`pnpm build` workflow. It does not replace the running Gateway or change its
configuration. After a full successful build it writes
`data/openclaw/source/.athena-build-ready.json`, recording source identity,
artifact and patch checksums, and completed checks. It removes the previous
marker before rebuilding, so failed builds cannot be selected as ready.
Subsequent offline rebuilds can omit `--restore --install`.
Node must already be installed at the pinned local runtime path.

Launch a validated source build through `data/openclaw/source/openclaw.mjs`,
using the same isolated ATHENA configuration and environment as the stable
runtime. Do not launch `src/index.ts` through `tsx`: upstream documents that
this compiles the bundled plugin graph on each process startup. Keep one
Gateway process running and reuse its authenticated WebSocket connection.

## Local source patches

`patches/0001-windows-native-build.patch` makes native Node build steps explicitly
bypass `cmd.exe`. The upstream build passed file URL preloads containing `%20`
for this project's spaces into the shell, which correctly rejected percent
characters. The patch fixes the command producer instead of weakening shell
escaping. Both normal Node steps and the existing no-pnpm Node fallback are
covered; pnpm command-shim handling is unchanged.

Useful ATHENA capabilities belong in `athena-plugin`, not hardcoded in OpenClaw
core. Configuration narrows activation to that plugin and the selected voice
agent. Persistent WebSocket requests eliminate process-per-message startup;
worker sessions keep long tasks separate from voice conversation. No model,
provider, persistent database schema, protocol version, or Gateway pacing
change is needed for those optimizations.

## Streaming contract and latency

The pinned Gateway's `src/gateway/server-chat.ts` broadcasts initial assistant
text immediately and coalesces subsequent live text at 75 ms. A chat delta has
`runId`, `sessionKey`, `seq`, `state: "delta"`, incremental `deltaText`, and
`message.content` containing the cumulative assistant text. `replace: true`
means that cumulative text changed rather than appended. New assistant items
after tools are prefixed by earlier text at the Gateway producer.

Clients consume either cumulative text or incremental text, never both. Track
the run's sequence and emitted text, discard duplicate events, and update the
display on replacement. Already spoken audio cannot be retracted; do not
replay earlier text to compensate. Terminal events reconcile remaining text.
The voice adapter owns phrase-boundary buffering and speech interruption.

Measure warm first-text and first-audio latency with the same model and speech
settings. Model/network latency remains independent of the Gateway's text
pacing. Reducing a pacing constant without a measured bottleneck is not an
optimization. Profile cold startup separately from a warm persistent session.

Protocol reference: https://docs.openclaw.ai/gateway/protocol

## Validated initial build

The first local source build completed successfully in 15 minutes. The native
Windows build regression suite passed all 77 tests; the CLI bootstrap guard,
148 public plugin SDK exports, runtime postbuild checks, and Control UI
performance guards passed. The launcher reported `OpenClaw 2026.8.1 (ea80657)`.
That first build used the machine's pnpm `11.25.0` with the frozen release lock;
the rebuild helper now uses the installed release-pinned pnpm `12.1.0`.
No installed runtime files were edited.
