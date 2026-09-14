# ATHENA Voice Command and Output Logic

## 1. Goal

ATHENA should feel responsive even when a cloud service is not instant. The target is:

- Wake feedback in under 80 ms.
- Final transcript shortly after the user stops speaking.
- First DeepSeek text around 500 ms after the transcript is ready.
- First audible answer around 900 ms after the user stops speaking.
- Immediate interruption when the user talks over ATHENA.
- No tool is reported as successful until its result confirms success.

The Orange Pi is the coordinator. Alibaba Cloud Model Studio in the Beijing region provides both streaming speech recognition and streaming speech synthesis. The official DeepSeek API remains the intelligence and tool-selection provider. This requires one Qwen/Alibaba key for all speech plus the existing official DeepSeek key.

## 2. One always-running program

Use one Python `asyncio` application with four long-lived workers:

```text
Audio worker       microphone frames -> wake word/VAD -> STT queue
STT worker         Qwen Audio ASR WebSocket -> partial/final transcripts
Agent worker       transcript -> local fast path or official DeepSeek/tools
Speech worker      text clauses -> Qwen Audio TTS -> speaker
```

Do not create a new Python process or reconnect to Alibaba for every command. Connections and HTTP clients should be established at startup and kept ready. A bounded `asyncio.Queue` connects each worker. Every event carries a `turn_id`; events from cancelled or older turns are discarded.

The microphone callback must never make network requests, write to the database, or wait for another component. It only places 20 ms PCM frames into a queue.

## 3. State machine

```text
SLEEPING
   | wake word
   v
ACTIVATED ---- immediate local activation sound
   | speech starts
   v
LISTENING ---- Qwen Audio ASR receives audio continuously
   | final transcript
   v
ROUTING
   | exact safe local command ------> LOCAL_ACTION
   | ordinary conversation ---------> THINKING
   | model requests tool -----------> USING_TOOL -> THINKING
                                                \
THINKING -> text clause -> TTS_STREAMING -> PLAYING -> SLEEPING

Any active state + new wake word or barge-in -> CANCEL_CURRENT -> LISTENING
Any cloud failure -> local error sound/message -> reconnect -> SLEEPING
```

Only one foreground turn may speak or execute tools at a time. Background timers and monitors are separate jobs and publish notifications through the same speech queue.

## 4. Wake-word behavior

Keep a circular buffer containing the most recent 800 ms of microphone audio. This prevents the beginning of a command from being cut off when someone says, “ATHENA, turn on the lights.”

On wake detection:

1. Create a new `turn_id` and cancel stale speech/output.
2. Turn on an LED and optionally play a quiet 30–50 ms nonverbal activation sound immediately.
3. Begin or continue sending microphone frames to Qwen Audio ASR.
4. Start a special 900 ms wake-follow-up window. Do not speak during this window.
5. If more speech arrives, treat the wake word and command as one utterance, suppress the catchphrase, strip the wake word from the transcript, and process the command.
6. Only if the follow-up window ends in silence and the recognized utterance contains only the wake word, play a cached response such as “Yes?” and keep listening for the command.

The activation sound and catchphrases are local WAV files. Qwen Audio TTS should generate them once during setup, not on every wake-up.

The program cannot know at the instant it hears “ATHENA” whether the user is about to continue speaking. An immediate spoken catchphrase will therefore always have some risk of interruption. The LED or very short nonverbal sound provides immediate confirmation; a verbal acknowledgement is reserved for a confirmed standalone wake word. Set `wake_followup_ms` in configuration so it can be tuned between roughly 700 and 1,200 ms for the user's speaking style.

Examples:

```text
“ATHENA, turn on the lights.”
    -> activation LED/sound, no catchphrase, command executes

“ATHENA ... could you turn on the lights?”
    -> continued speech arrives inside grace window, no catchphrase

“ATHENA.” [silence for 900 ms]
    -> “Yes?” and a fresh command-listening window
```

## 5. Listening and deciding when speech ended

Capture mono PCM at 16 kHz, 16-bit, in 20 ms frames. Use local voice activity detection only to control the turn; Qwen Audio ASR remains responsible for the words.

Suggested endpoint rules:

- Ignore isolated noise shorter than 80 ms.
- Consider speech started after 100 ms of voiced audio.
- End a normal command after 220 ms of silence when Qwen Audio ASR has a stable partial transcript.
- Extend the silence limit to 450 ms after conjunctions or incomplete phrases such as “and”, “because”, or “then”.
- Hard-stop a turn after 15 seconds unless dictation mode is active.
- Qwen Audio ASR's final result is authoritative; never send an unstable partial transcript to a tool.

While the user is speaking, prepare the DeepSeek request envelope, recent-memory context, and available-tool definitions. This work is completed before the final transcript arrives.

## 6. Routing commands

Use two routes, checked in this order.

### Route A: exact local fast paths

Handle only commands with an unambiguous grammar and no dangerous side effects:

- “stop”, “be quiet”, “cancel”
- “volume up/down”, “mute”
- “what time is it?”
- “set a timer for N minutes” after strict duration parsing

These do not need DeepSeek and can respond in tens of milliseconds. Do not build a broad keyword classifier. For example, the word “stop” inside “do not stop the music” must not activate the standalone stop command.

### Route B: Official DeepSeek

Everything else goes to the official DeepSeek API's `deepseek-v4-flash` with thinking disabled, streaming enabled, a short context, and native tool definitions.

DeepSeek returns one of two things:

```text
assistant text
tool call: {name, validated arguments}
```

Never ask one model call to produce informal JSON embedded in prose. Use API tool calling and validate every argument with Pydantic before execution.

## 7. Tool execution logic

Every tool has metadata:

```text
name
description
argument schema
timeout
permission level
whether it is cancellable
whether duplicate calls are safe
```

Permission levels:

```text
SAFE          run immediately: time, weather, read sensors
REVERSIBLE    run and announce: lights, volume, timers
CONFIRM       ask first: messages, purchases, deleting data, unlocking doors
BLOCKED       never expose to the model
```

Execution sequence:

1. Validate the tool name against the registry.
2. Validate and normalize its arguments.
3. Check permission policy.
4. Assign an idempotency key based on `turn_id + call_index`.
5. Run with a strict timeout.
6. Store the real result.
7. Send that result back to DeepSeek for the spoken answer.

If a tool normally finishes in under 700 ms, stay silent. If it takes longer, play one cached filler phrase such as “One moment.” Do not play filler for every command.

Never say “Done” when the tool returned an error or timed out.

## 8. Turning streamed text into speech

Do not wait for DeepSeek's full answer. Feed completed clauses to Qwen Audio's streaming TTS connection.

The clause chunker should emit when one of these conditions is met:

- It sees `.`, `?`, `!`, `;`, or a natural comma after at least 24 characters.
- The buffer reaches about 70 characters and contains a safe word boundary.
- DeepSeek finishes and text remains buffered.

Do not emit:

- An unfinished number, date, URL, abbreviation, or Markdown marker.
- Model text while a possible tool call is still being assembled.
- More than two queued speech clauses; excessive queued speech makes interruption feel slow.

DeepSeek's voice prompt should require the answer's first clause to be independently useful and short. Example:

```text
Good: “The lights are on.”
Slow: “Certainly. I would be more than happy to help you with that request.”
```

Audio playback begins with the first PCM packet from Qwen Audio TTS while later text and audio are still being generated.

## 9. Interruption and echo

ATHENA must listen while speaking. When the microphone detects human speech during playback:

1. Require either the wake word or 180 ms of confident near-field speech.
2. Stop the speaker buffer immediately.
3. Cancel the active TTS stream.
4. Cancel the DeepSeek stream if it is only generating speech.
5. Cancel a running tool only if the tool declares itself cancellable.
6. Start a new turn and discard old events by `turn_id`.

Software echo cancellation is useful, but a USB speakerphone with hardware echo cancellation will be more dependable on a small SBC. Until echo cancellation is working, require the wake word to interrupt ATHENA while it is speaking.

## 10. Latency budget

Measured from the end of the user's speech:

| Stage | Target |
|---|---:|
| Local endpoint decision | 120–220 ms |
| Qwen Audio ASR final transcript | 80–250 ms |
| DeepSeek first useful clause | 450–550 ms |
| Qwen Audio TTS first audio packet | 100–200 ms |
| Audio buffering | 40–60 ms |
| First audible answer | 790–1,230 ms |

The under-one-second goal is achievable on good network turns, but cannot be guaranteed by free cloud APIs. The Orange Pi should add very little latency if audio callbacks and queues never block.

## 11. Startup and recovery

At startup, perform these tasks concurrently:

- Load configuration and cached audio.
- Open the microphone and speaker.
- Connect Qwen Audio ASR and TTS streams in Alibaba's Beijing region.
- Create a persistent client for the official DeepSeek API.
- Load the tool registry and the last conversation summary.
- Make one tiny DeepSeek warm-up request.

Recovery rules:

- Reconnect a dropped WebSocket with exponential backoff capped at 5 seconds.
- Play a local error phrase if speech output is unavailable.
- Keep local stop, volume, timer, and GPIO commands operational during a cloud outage.
- Never replay a non-idempotent tool automatically after an uncertain network failure.

## 12. Queue and event design

Core events:

```python
WakeDetected(turn_id, timestamp)
SpeechStarted(turn_id, timestamp)
TranscriptPartial(turn_id, text, stability)
TranscriptFinal(turn_id, text, confidence)
ModelText(turn_id, text)
ToolRequested(turn_id, call_id, name, arguments)
ToolCompleted(turn_id, call_id, result)
SpeechClause(turn_id, text)
AudioChunk(turn_id, pcm)
TurnCancelled(turn_id, reason)
```

Recommended queue limits:

- Microphone frames: 100 frames, or 2 seconds.
- Model text events: 50.
- Speech clauses: 2.
- TTS audio: 500 ms of audio.

If audio output falls behind, cancel old speech instead of allowing several seconds of stale narration to accumulate.

## 13. Measurements required on every turn

Record monotonic timestamps for:

- Wake detected.
- Speech started and ended.
- First and final STT text.
- DeepSeek request, first text, and completion.
- Tool start and finish.
- First TTS audio received.
- First audio played and playback completed.
- Cancellation response time.

Print a compact line after each turn and store it in SQLite. Optimize the measured slowest stage rather than guessing.

## 14. Implementation order

1. Replace the Azure prototype with Alibaba Qwen Audio streaming ASR and TTS provider classes.
2. Build microphone and speaker workers with bounded queues.
3. Add the state coordinator and `turn_id` cancellation.
4. Stream official DeepSeek answers through the clause chunker into Qwen Audio TTS.
5. Add exact local fast paths.
6. Add the validated tool registry and permission checks.
7. Add wake-word detection and cached acknowledgements.
8. Add barge-in and echo cancellation.
9. Tune endpoint timing using recorded latency data.

Do not begin with wake-word tuning. First make push-to-talk streaming reliable; then replace the button with the wake-word state transition.
