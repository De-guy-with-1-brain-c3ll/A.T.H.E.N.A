#!/usr/bin/env bash
# Install Piper on the Pi so speech costs nothing per character.
#
#   sudo bash tools/install_piper.sh [voice]
#
# Piper runs on CPU-only ARM at roughly eight times realtime, so an Orange Pi
# synthesizes faster than it speaks. After this, set ATHENA_TTS_BACKEND=piper in
# /etc/athena/athena.env and restart the voice service.
set -euo pipefail

VOICE="${1:-en_US-lessac-medium}"
VOICES_DIR="${ATHENA_PIPER_VOICES:-/opt/athena/voices}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${REPO_DIR}/.venv/bin"

echo "Installing Piper and the ${VOICE} voice into ${VOICES_DIR}"

apt-get update
# espeak-ng-data is what turns text into phonemes; Piper is useless without it.
apt-get install -y python3-pip espeak-ng-data

# piper-tts ships the binary as a console script inside the virtual environment.
if [ -x "${VENV}/pip" ]; then
  "${VENV}/pip" install --upgrade piper-tts
else
  python3 -m pip install --break-system-packages --upgrade piper-tts
fi

mkdir -p "${VOICES_DIR}"
# huggingface.co is unreachable from some networks (it answers 502), and the
# mirror serves the same repository. Override with PIPER_MODEL_BASE if you have
# a faster mirror or a local copy.
BASE="${PIPER_MODEL_BASE:-https://hf-mirror.com/rhasspy/piper-voices/resolve/main}"
LANG_CODE="${VOICE%%_*}"                 # en
REGION_AND_NAME="${VOICE#*_}"            # US-lessac-medium
NAME="${REGION_AND_NAME#*-}"             # lessac-medium
QUALITY="${NAME##*-}"                    # medium
SPEAKER="${NAME%-*}"                     # lessac
# Voices are laid out as <lang>/<lang_region>/<name>/<quality>/, for example
# en/en_US/lessac/medium/. The region keeps the case it has in the voice name —
# lowercasing it was wrong: the repository answers 404 for en/en_us/..., which
# reads like the mirror being broken when the path is simply misspelled.
LANG_REGION="${VOICE%%-*}"               # en_US
FOLDER="${LANG_CODE}/${LANG_REGION}/${SPEAKER}/${QUALITY}"
for suffix in onnx onnx.json; do
  target="${VOICES_DIR}/${VOICE}.${suffix}"
  if [ -f "${target}" ]; then
    echo "  ${target} already present"
    continue
  fi
  echo "  fetching ${VOICE}.${suffix}"
  if ! curl -fsSL -o "${target}.part" "${BASE}/${FOLDER}/${VOICE}.${suffix}"; then
    rm -f "${target}.part"
    echo "  download failed from ${BASE}" >&2
    echo "  try again, or set PIPER_MODEL_BASE to another mirror" >&2
    exit 1
  fi
  mv "${target}.part" "${target}"
done

# A voice is only usable if both halves arrived.
for suffix in onnx onnx.json; do
  if [ ! -s "${VOICES_DIR}/${VOICE}.${suffix}" ]; then
    echo "  ${VOICE}.${suffix} is empty; the voice is not usable" >&2
    exit 1
  fi
done

chown -R athena:athena "${VOICES_DIR}" 2>/dev/null || true

echo
echo "Checking the voice actually speaks:"
if command -v piper >/dev/null 2>&1; then
  PIPER_BIN="$(command -v piper)"
elif [ -x "${VENV}/piper" ]; then
  PIPER_BIN="${VENV}/piper"
else
  PIPER_BIN=""
fi

if [ -n "${PIPER_BIN}" ]; then
  echo "  binary: ${PIPER_BIN}"
  echo "  This is a test of the local voice." \
    | "${PIPER_BIN}" --model "${VOICES_DIR}/${VOICE}.onnx" --output-raw \
    | wc -c | sed 's/^/  bytes of audio produced: /'
else
  echo "  piper binary not found on PATH; set ATHENA_PIPER_BINARY in athena.env"
fi

echo
echo "Now add these to /etc/athena/athena.env and restart athena-voice:"
echo "  ATHENA_TTS_BACKEND=piper"
echo "  ATHENA_PIPER_VOICES=${VOICES_DIR}"
echo "  ATHENA_PIPER_VOICE=${VOICE}"
[ -n "${PIPER_BIN}" ] && echo "  ATHENA_PIPER_BINARY=${PIPER_BIN}"
