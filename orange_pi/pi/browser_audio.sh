#!/usr/bin/env bash
# Turn on ATHENA's browser microphone and speaker on the local network.
# Runs on the Orange Pi. Safe to re-run: every step checks the current state.
set -uo pipefail

ENV_FILE=/etc/athena/athena.env
ENABLE="${1:-on}"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

if [ "$ENABLE" = "off" ]; then
  VALUE=0
else
  VALUE=1
fi

say "Browser audio: $([ "$VALUE" = 1 ] && echo on || echo off)"
cp -n "$ENV_FILE" "$ENV_FILE.bak-browser-audio" 2>/dev/null || true
if grep -q '^ATHENA_REMOTE_AUDIO=' "$ENV_FILE"; then
  sed -i "s/^ATHENA_REMOTE_AUDIO=.*/ATHENA_REMOTE_AUDIO=$VALUE/" "$ENV_FILE"
else
  printf '\n# Microphone and speaker supplied by a device on the local network.\n# Start and stop it from the dashboard; no extra port is opened.\nATHENA_REMOTE_AUDIO=%s\n' "$VALUE" >> "$ENV_FILE"
fi
grep -E '^ATHENA_REMOTE_AUDIO' "$ENV_FILE" | sed 's/^/  /'

say "Restarting the voice service"
systemctl restart athena-voice.service
sleep 8
if systemctl is-active --quiet athena-voice.service; then
  echo "  athena-voice.service is running."
else
  echo "  athena-voice.service FAILED. Last lines:"
  journalctl -u athena-voice.service -n 15 --no-pager | tail -15
fi

if [ "$VALUE" = 1 ]; then
  say "Open the dashboard on the device you want to use"
  lan=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -n "${lan:-}" ] && echo "  http://$lan:8780"
  echo
  echo "Sign in, then press Start under Microphone and speaker on the Command tab."
  echo "Use headphones, or the microphone will hear ATHENA's own voice."
else
  echo
  echo "ATHENA is back on the Pi's own microphone and speaker."
fi
