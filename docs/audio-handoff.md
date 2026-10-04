# Live Pi/computer audio handoff

The voice process now exposes one stable microphone/speaker pair backed by an
atomic router. Both endpoints move together, and music continues using the same
speaker facade. Failed destination-device opens leave the current pair intact.
An already-decoded stereo music packet is downmixed/resampled for the Pi's mono
stream on handoff. No model call is needed for common voice commands.

On the computer, open the Pi's **HTTPS** control page, find **Microphone and
speaker**, click **Start**, and grant microphone permission. Keep the page open.
This uses the browser/OS default input and output—not arbitrary Windows-wide
device settings. While Pi mode is selected the armed browser does not forward
microphone audio; its local microphone remains open so a future handoff can work
without another permission prompt. Click Stop to release it entirely.

Say “switch your audio output to my computer” to switch **both** directions, or
“switch back to the Pi.” Confirmation plays on the new destination. Buttons
**Use computer** and **Use Pi** are also in the same dashboard card. Pi mode uses
the configured `ATHENA_AUDIO_INPUT_DEVICE` / `ATHENA_AUDIO_OUTPUT_DEVICE`; both
devices must actually be available. When no computer audio connection is ready,
the switch is rejected rather than pretending it succeeded.

The authenticated dashboard API is `/api/audio-route` (GET status, POST with
`target: computer|pi`, requiring the normal CSRF protection). Model/tool requests
go through the local Unix control socket and a bounded handoff queue, so they
cannot mutate devices halfway through a spoken reply. Handoffs are runtime
changes; a restart uses the existing `ATHENA_REMOTE_AUDIO` startup preference.
If the computer disconnects while selected, use the dashboard's Use Pi button
to return to the Pi; disconnect is not silently treated as consent to start a
different microphone.
