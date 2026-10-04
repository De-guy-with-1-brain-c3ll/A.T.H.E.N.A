# Music / speech flow fix (.56)

Root cause: the normal streamed-answer path did not suspend music, although
the separate canned-reply path did. Both producers shared a lock for individual
PCM writes, not for a complete spoken reply. This alternated music and speech
packets and repeatedly switched remote audio formats. Manual pause stopped
ffmpeg but did not gate audio already decoded into its pipe.

## Revised flow

- Music may continue during model thinking and synthesis startup. The first
  ready speech packet acquires the audio floor; all following speech packets
  retain it until the last packet finishes playing.
- At handoff, wait for at most the current 100-ms music packet, then discard
  queued remote music. On the browser, account for its final-packet playback
  and 60-ms scheduling lead before releasing the floor.
- Nested voice paths use a hold count. An inner reply cannot resume music
  while another voice owner still holds the floor. Cancellation flushes the
  old reply and releases ownership.
- Manual pause gates actual PCM output, independent of decoder buffering and
  operating-system signals. Completing speech cannot undo a manual pause.
- Resume, next and stop preserve this distinction. Skip discards old packets
  waiting behind speech. Decoder shutdown drains killed-process pipes to avoid
  hanging when ffmpeg is backpressured.
- Physical speaker writes are capped at 100 ms. Abort uses the actual PyAudio
  low-level binding, since PyAudio.Stream has no abort_stream method.
- Music PCM is aligned to complete samples/channel frames before scaling.

## Agent behaviour and API costs

Pause/resume/skip/stop voice commands operate locally before a short verified
confirmation; they do not create an agent or request DeepSeek. Confirmed local
control turns are retained in conversation memory. Play/search remains a
background operation. Tool replies are shorter, and the voice prompt asks for
action first, concise verified results and no narration of individual tool calls.
Approval and action-verification safeguards remain in place.

With the installed local streaming wake model, "ATHENA, pause music" can activate
while music is playing. Music audio without the keyword does not open cloud STT,
including during the normal follow-up window. While music is audible, say the
wake word; after pausing it, ordinary active-window follow-ups work again.
Edge TTS and Qwen STT remain the primary providers. These changes do not add
per-turn model requests or automatic paid prompt evaluations.

## Verification

Full local run: 792 tests successful with 10 optional skips. Pi regression run:
204 tests passed, including the real decoder integration (no skips). No paid
model/STT evaluation calls were made during these playback regressions.

Regression coverage includes contiguous streamed speech versus concurrent music,
nested ownership, manual pause during speech, buffered-write pause, stale-track
skip, cancellation, browser final-packet timing, USB abort/write bounds, local
fast controls, and keyword-gated music commands without idle cloud audio.

A real ffmpeg integration test runs on the Pi with a generated 30-second tone
and an instrumented sink. It verifies pause, two contiguous speech packets,
remaining paused, resume and bounded full-pipe shutdown. This does not play
through the user's physical speakers. Room acoustics and perceived transition
quality still need the user's listening check.
