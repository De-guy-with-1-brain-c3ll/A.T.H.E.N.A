# Approved JARVIS-style interactions (.55)

The 30 approved scenarios, context and target speech are packaged with ATHENA
in `src/athena/system/interaction_examples.json`. Two contain a restrained dry
aside. These are fictional evaluation/reference scenarios, never live facts or
memories. Their numbers, deadlines and news must not leak into real replies.

The runtime style is distilled into `interaction_style.txt` and added to both
voice and text prompts. All 30 dialogues are not resent on every turn: only
the compact policy and a few short tone examples are sent, preserving token
savings. Existing customized system prompts are not overwritten.

Desired behaviour: useful answer first, short spoken replies, understated
confidence, occasional humour, and an occasional "sir" rather than one in every
sentence. No jokes during failures, approvals, urgent alerts or user frustration.
Never invent successful actions, capability limitations or system-wide failures.

## Tuning and tests

First bounded live DeepSeek round: 24/27 content checks passed. Manual review
also found excessive honorifics and an invented coding limitation. The shared
prompt was revised, not swapped for a different live prompt on every request.

Second round: honorifics and invented limitation improved. Two checks needed
review: "Standing by" was a valid waiting response omitted from the checker;
"Ten minutes. I'll be here" did not clearly confirm the saved alarm. The
checker accepts the legitimate equivalent, while the prompt now explicitly
confirms saved alarms. Only cases 5, 28 and 29 were retested to avoid another
full billed run. Latest available results: 27/27 model content checks pass.

These checks evaluate required facts, contradictions, brevity and status wording;
they are not proof of cinematic personality or word-for-word equivalence.
Lexical similarity is recorded for review, not treated as a correctness score.
The model is allowed to phrase the same verified information naturally.

Three scenarios (wake-only, TV/background rejection, approval rendering) are
engine references rather than model completions. Existing coordinator/approval
tests cover those mechanisms; speaker/TV discrimination is not guaranteed in
real-room acoustics. Wake responses retain ten variants, with one "Yes, sir?".
Explicit "that's enough for now", "go quiet", "stop listening" and "standby"
close the active window without stopping the program. The ten-second alarm
sound remains unchanged.ta

Full regression run: 779 tests, successful with 9 optional skips. Five new tests
cover reference completeness, sparse humour guidance, fictional-state exclusion,
custom prompt preservation and quiet-mode routing. The existing Windows
asyncio-loop ResourceWarning remains a test-harness limitation.

Raw live evaluations are local ignored artifacts under `outputs/interactions`.
`tools/interaction_qa.py` makes bounded billed requests and never executes tools;
`--ids` retests only selected cases. Normal use does not run these evaluations.
