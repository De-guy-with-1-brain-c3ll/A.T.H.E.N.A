# VO Scripts — click-synced narration

Click cues are marked **[C]**. Each deck lists its step count so you can pace: ~53 clicks total, roughly 3 minutes at a normal talking speed. Don't rush clicks — the camera needs ~1.8s to land before you start on the new element.

---

## 05 — Kinetic Title (5 clicks) · opener, ~20s

**[C]** What you're looking at is JARVIS — on a credit-card computer.
**[C]** Not a demo. Not a chatbot in a terminal. A voice assistant.
**[C]** (beat — let the rule sweep land)
**[C]** And it answers in 1.2 seconds. Real, measured — not a claim in a README.
**[C]** Build 45. Let me show you how.

---

## 01 — Architecture (11 clicks) · ~45s

**[C]** The whole thing is one asyncio process on an Orange Pi Zero 3 with 4 gigs of RAM.
**[C]** Four workers. First: Audio — it grabs the mic and streams chunks.
**[C]** STT — speech to text. **[C]** Agent — the brain, it decides what to do. **[C]** Speech — text back out to your ears.
**[C]** Median round trip, question to first spoken word: 1.17 seconds.
**[C]** Mic and speaker endpoints — that's your actual voice in, voice out.
**[C]** The workers are wired together with queues — audio flows down this line.
**[C]** Speech recognition runs on Qwen's ASR in the cloud — **[C]** the brain is DeepSeek — **[C]** and the voice is Qwen TTS.
So the Pi does the real-time work, the cloud does the heavy lifting.

---

## 02 — State Machine (10 clicks) · ~40s

**[C]** Every turn is a state machine. It starts asleep.
**[C]** Wake word hits — LED on, chirp played, in under 80 milliseconds.
**[C]** Listening. **[C]** Then routing: does this need the cloud, or can the Pi handle it?
**[C]** Simple stuff — timers, lights — happens locally.
**[C]** Otherwise it's thinking. **[C]** Then TTS streams — **[C]** and plays while it's still downloading, that's the trick to the speed.
**[C]** And the loop just keeps going.
**[C]** But here's the part I'm proud of: interrupt it mid-sentence —
**[C]** it cancels by turn ID. The stale audio dies, it starts listening instantly.

---

## 03 — Counters (5 clicks) · ~30s

**[C]** Four numbers, on the real Pi — every one from a real benchmark file, and I'll show you the terminal in a second.
**[C]** The headline: **[C]** 1.17 seconds, end to end. Ask it something, it starts talking before most assistants finish *thinking*.
**[C]** 80 milliseconds from finished sentence to first audio chunk. **[C]** 738 tests passing — because this thing talks out loud, you don't get silent bugs, you get weird ones. **[C]** And 9 days uptime on the Pi — it just runs.

---

## 04 — Terminal (9 clicks) · ~40s

**[C]** This is the actual benchmark script, running on the Pi.
**[C]** One clip through the whole pipeline...
**[C]** STT final: 0.75 seconds. **[C]** LLM first text: just under a second — that's running concurrent with the STT tail, not after it. **[C]** Text to first PCM: 80 milliseconds. **[C]** Add it up — 1.17 median.
**[C]** But look at the comment at the bottom. This one cost me an evening.
**[C]** I tried tuning the cloud TTS — pitch up 2 hertz so it sounds younger.
**[C]** That one parameter added 2.3 seconds to every single reply. The request just sat there.
**[C]** Fix: delete it. 0.85 back down — 2.3 seconds back. Some snags you debug. Some you just... let go.

---

## 08 — Latency Bars (5 clicks) · ~30s

**[C]** Where does the 1.17 actually go? Same numbers, to scale. The bar is speech recognition — it's most of the budget, and it's the part I can't make faster.
**[C]** The LLM finishes at 0.98 — but it *starts* before STT is done, off partial text.
**[C]** First full clause lands at 1.09. **[C]** And text-to-audio is basically free — 80 milliseconds.
**[C]** That's the whole design in one picture: the voice starts 80 milliseconds after the words exist, and everything overlaps.

---

## 06 — Snags (4 clicks) · ~30s

**[C]** Okay, failures — because the demo version of this video is lying to you if I skip these. First local STT attempt: SenseVoice — great accuracy, 326 percent CPU. The Pi became a space heater.
**[C]** Then Kitten TTS — 0.3× realtime. It would've spoken faster if I just... typed.
**[C]** Piper loaded in 13.3 seconds. Longer than the conversation it was joining.
**[C]** So the voice went to the cloud — and the round trip ended up at 1.17 seconds anyway. Sometimes the boring solution is the fast one.

---

## 07 — Tools (4 clicks) · ~35s

**[C]** Speed means nothing if it can't *do* anything. Twenty registered tools, one registry, every call permission-tiered. Read-only tier: weather, time, search, browsing — it can look, it can't touch.
**[C]** Reversible tier: alarms, timers, music, sleep. It can change state — but I can undo it.
**[C]** Consequential tier — red, on purpose: running commands, downloading files, shutdown. These validate, log, and get treated like they're dangerous, because they are.
**[C]** An assistant that talks fast is a toy. An assistant where every action is tiered and logged — that's one you leave plugged in.
