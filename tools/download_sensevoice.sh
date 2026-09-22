#!/usr/bin/env bash
# Download SenseVoice-Small so ATHENA can recognise speech without the cloud.
#
#   bash tools/download_sensevoice.sh
#
# Speech recognition is billed per second of audio on every cloud model, so the
# only way to stop paying is to stop sending audio anywhere. This fetches the
# model once; after it, set ATHENA_STT_BACKEND=sensevoice in
# /etc/athena/athena.env and restart the voice service.
set -euo pipefail

DEST="${ATHENA_SENSEVOICE_MODEL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/outputs/models/sensevoice}"

# huggingface.co answers intermittent 502 from some networks, and the mirror
# serves the same repository. Override with SENSEVOICE_MODEL_URL if you have a
# faster mirror or a local copy.
BASE="${SENSEVOICE_MODEL_URL:-https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main}"

# int8 is the default and the right choice on a small board: 228 MB instead of
# 938 MB and about 2.5x faster, for a word error rate of 0.188 against 0.171.
# Set ATHENA_SENSEVOICE_PRECISION=fp32 to fetch the full-precision model too.
PRECISION="${ATHENA_SENSEVOICE_PRECISION:-int8}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "${DEST}"

fetch() {
  local name="$1"
  local target="${DEST}/${name}"
  if [ -s "${target}" ]; then
    echo "  ${name} already present ($(wc -c <"${target}") bytes)"
    return 0
  fi
  echo "  fetching ${name}"
  # Download to .part and only move on success. A truncated .onnx does not fail
  # at download time: it surfaces later as a protobuf parse error, which reads
  # like a corrupt model rather than an interrupted fetch.
  if ! curl -fL --retry 2 -o "${target}.part" "${BASE}/${name}"; then
    rm -f "${target}.part"
    echo "  download failed from ${BASE}" >&2
    echo "  try again, or set SENSEVOICE_MODEL_URL to another mirror" >&2
    return 1
  fi
  mv "${target}.part" "${target}"
}

echo "Installing SenseVoice-Small into ${DEST}"

fetch tokens.txt
fetch model.int8.onnx
if [ "${PRECISION}" = "fp32" ] || [ "${PRECISION}" = "both" ]; then
  fetch model.onnx
fi

# Both halves are required: the model is meaningless without its token list, and
# an empty file means an interrupted download that must not be left in place.
for name in tokens.txt model.int8.onnx; do
  if [ ! -s "${DEST}/${name}" ]; then
    echo "  ${name} is empty; the model is not usable" >&2
    exit 1
  fi
done

echo
echo "Checking the model actually recognises speech:"
# The virtual environment ATHENA actually runs from is the one that has to have
# sherpa-onnx; the system interpreter usually does not. On the Pi that is the
# active release's own venv, because the updater builds a fresh one per release
# rather than reusing anything under the source checkout.
PYTHON=""
for candidate in \
  "${REPO_DIR}/.venv/bin/python" \
  "${REPO_DIR}/.venv/Scripts/python.exe" \
  /opt/athena/current/.venv/bin/python \
  "$(command -v python3 || true)" \
  "$(command -v python || true)"; do
  if [ -n "${candidate}" ] && [ -x "${candidate}" ] \
     && "${candidate}" -c 'import sherpa_onnx' 2>/dev/null; then
    PYTHON="${candidate}"
    break
  fi
done
if [ -n "${PYTHON}" ]; then
  "${PYTHON}" - "${DEST}" <<'PY'
import sys, time
from pathlib import Path
import sherpa_onnx

folder = Path(sys.argv[1])
started = time.perf_counter()
recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=str(folder / "model.int8.onnx"),
    tokens=str(folder / "tokens.txt"),
    num_threads=2, use_itn=True, language="auto", debug=False)
load = time.perf_counter() - started
# Silence: it must decode (proving the model is real and loaded) without hanging.
stream = recognizer.create_stream()
stream.accept_waveform(16000, [0.0] * 16000)
recognizer.decode_stream(stream)
print(f"  model loaded in {load:.2f}s and answered a decode")
PY
else
  echo "  sherpa-onnx is not importable by any interpreter tried. Install it with:"
  echo "    /opt/athena/current/.venv/bin/python -m pip install sherpa-onnx"
  echo "  The release installs it from pyproject.toml, so this usually means the"
  echo "  update has not been pulled yet."
fi

echo
echo "Now add these to /etc/athena/athena.env and restart athena-voice:"
echo "  ATHENA_STT_BACKEND=sensevoice"
echo "  ATHENA_SENSEVOICE_MODEL_DIR=${DEST}"
echo "  ATHENA_SENSEVOICE_THREADS=2"
echo
echo "Set ATHENA_STT_BACKEND back to qwen to return to the cloud recogniser."
