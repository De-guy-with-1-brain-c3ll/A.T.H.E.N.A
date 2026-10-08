# Athena repair validation — 7 October 2026

Release: `0.1.0-pi.88`. Pi: `192.168.33.153`. PC inbox: `192.168.33.186:8781`.

## Repairs

- Weather, homework, coding, music and VPN requests receive their tool schemas immediately. Clear requests call the relevant tool; action promises require execution evidence.
- “Nettie's” is interpreted as NetEase. Platform changes and affirmative follow-ups retain the requested song.
- NetEase search and audio use direct VPN routing; YouTube uses the configured VPN. YouTube requires a reachable connection when the VPN is stopped.
- Number words work in timer durations.
- Audio return commands such as “switch it back to my computer” work. The desktop microphone connection remains available while the Pi route is selected.
- Stop commands can interrupt speech using a separate recognizer, including partial transcripts and the recognizer's “stopped talking” inflection.
- Edge Ava provides expressive English speech. Speech formatting removes comma delays and converts written lists; synthesis and playback have separate completion limits.
- Saved coding and downloaded files are recorded across interfaces and restarts. Transfers report percentage, bytes and a verified receipt. Authenticated inbox discovery handles PC DHCP changes.
- Teams sorts current work ahead of old assignments, resolves class names and reads instructions where accessible. Student accounts can filter their own assignment collection when class access is denied.
- Coding uses system Python inside Bubblewrap instead of the private service virtualenv, which the sandbox deliberately cannot access.
- Bubblewrap uses an empty `/proc` and only inert standard devices. The user approved disabling the incompatible `RestrictSUIDSGID` service flag. Filesystem and kernel protections remain. A root-owned VPN socket authenticates the local peer and permits only the existing controller's fixed actions, allowing voice to retain `NoNewPrivileges`.
- PC browser opening uses the authenticated PC bridge and reports actual navigation results.

## Verification

- Full local regression: 995 tests, nine skipped. Tests use fake audio outputs.
- Real Pi interruption recognition: “Athena stop talking” about 2.2 seconds; “Stop” about 1.1 seconds; “Be quiet” about 1.3 seconds. Generated input was never played.
- Real Teams: all assignments and Physics filtering returned ten rows, including accessible instructions.
- Real NetEase and YouTube: AC/DC “Back in Black” decoded into a silent sink. Both worked with corrected VPN routing. VPN restored to stopped.
- Live model: exact VPN stop, “Nettie's music,” and affirmative follow-up requests called the correct tools and succeeded.
- Edge synthesis: first PCM about 1.0–1.1 seconds; one list and three successive clauses completed with 403,200 PCM bytes each.
- Six real transfers: three sandbox-created programs and three public downloads. Received files independently matched sizes and SHA-256 checksums. Additional live model transfers returned 100% and byte counts.
- Actual dashboard service ran the Hello World program successfully before transferring it. A temporary service with the voice service's protections passed syntax and run checks and VPN stop. The test tool correctly reported failure for a project with no tests.
- VPN socket tests rejected an unauthorized peer, shell command injection and malformed endpoints.
- Real PC browser opened Example Domain and returned running status.
- Installed release passed silent computer → Pi → computer routing; restored to Pi.

No speaker playback was used. Physical loudspeaker echo and room acoustics were not tested. External services and networking can still fail; Athena must report those failures accurately. A finite test suite cannot establish that no bugs remain.
