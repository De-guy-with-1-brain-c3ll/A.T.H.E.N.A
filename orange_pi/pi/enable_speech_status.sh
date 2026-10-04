#!/bin/sh
# Turn on the speech-status file the dashboard reads.
set -e
ENV_FILE=/etc/athena/athena.env
KEY=ATHENA_AUDIO_STATUS_PATH

if [ ! -f "$ENV_FILE" ]; then
  echo "no $ENV_FILE on this box"
  exit 1
fi

if [ ! -f "$ENV_FILE.bak-speech" ]; then
  cp "$ENV_FILE" "$ENV_FILE.bak-speech"
  echo "backed up to $ENV_FILE.bak-speech"
fi

if grep -q "^$KEY=" "$ENV_FILE"; then
  sed -i "s|^$KEY=.*|$KEY=/run/athena/audio-status.json|" "$ENV_FILE"
  echo "$KEY set to /run/athena/audio-status.json"
else
  printf '\n%s=/run/athena/audio-status.json\n' "$KEY" >> "$ENV_FILE"
  echo "$KEY added"
fi

grep -n "^$KEY=" "$ENV_FILE"
