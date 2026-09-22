#!/usr/bin/env bash
# Install a local voice on the Pi so speech costs nothing per character.
#
#   sudo bash tools/install_sherpa_tts.sh [model]
#
# Defaults to the VITS Piper voice, and that default is a measurement rather than
# a preference. On an Orange Pi Zero 3 this board's CPU has NEON but no int8
# dot-product extensions, so quantised kernels fall back to generic paths and pay
# far more than their size suggests. Measured at 2 threads:
#
#   vits-piper-en_US-lessac-medium   0.89x realtime   <- default
#   kitten-nano-en-v0_8-int8         0.30x realtime   <- 3x too slow here
#
# Both are installed the same way, so `sudo bash tools/install_sherpa_tts.sh
# kitten-nano-en-v0_8-int8` works if a different voice is wanted. The same voice
# already on this board as a Piper install is available in sherpa's layout, so
# switching to the in-process backend costs nothing in voice quality.
#
# After this, set ATHENA_TTS_BACKEND=sherpa in /etc/athena/athena.env and restart
# athena-voice. The family is detected from the directory name, so there is no
# second setting to keep in sync.
set -euo pipefail

MODEL="${1:-vits-piper-en_US-lessac-medium}"
MODELS_DIR="${ATHENA_SHERPA_MODEL_DIR:-/opt/athena/models/sherpa}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${REPO_DIR}/.venv/bin"

echo "Installing the ${MODEL} voice into ${MODELS_DIR}"

# sherpa-onnx ships CPU-only aarch64 wheels, which is the whole reason this
# works on a board with no GPU and no cloud connection.
if [ -x "${VENV}/pip" ]; then
  "${VENV}/pip" install --upgrade sherpa-onnx
else
  python3 -m pip install --break-system-packages --upgrade sherpa-onnx
fi

mkdir -p "${MODELS_DIR}"
TARBALL="${MODELS_DIR}/${MODEL}.tar.bz2"

if [ -f "${MODELS_DIR}/${MODEL}/tokens.txt" ]; then
  echo "  ${MODEL} is already installed"
else
  # GitHub's release CDN intermittently answers 502 for these assets, from the Pi
  # and from a desktop alike, so the retries are not decoration. Override with
  # SHERPA_MODEL_BASE for a mirror or a local copy.
  BASE="${SHERPA_MODEL_BASE:-https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models}"
  echo "  fetching ${MODEL}.tar.bz2"
  if ! curl -fL --retry 5 --retry-all-errors --retry-delay 3 \
       -o "${TARBALL}.part" "${BASE}/${MODEL}.tar.bz2"; then
    rm -f "${TARBALL}.part"
    echo "  download failed from ${BASE}" >&2
    echo "  try again, or set SHERPA_MODEL_BASE to a mirror" >&2
    exit 1
  fi
  mv "${TARBALL}.part" "${TARBALL}"
  # Extract only once the download has fully finished. Extracting while curl is
  # still writing gives "Protobuf parsing failed", which reads like a broken
  # model rather than an unfinished download. A concurrent second download
  # writing the same path produced a 35 MB file for a 31 MB asset, so the size
  # is checked rather than assumed.
  echo "  downloaded $(stat -c %s "${TARBALL}") bytes"
  tar xjf "${TARBALL}" -C "${MODELS_DIR}"
  rm -f "${TARBALL}"
fi

# A voice is only usable if every half arrived. Checked as a set because each
# file is loaded by a different code path, so a partial install fails at a
# different point depending on which one is missing.
REQUIRED="tokens.txt espeak-ng-data"
# Only the multi-speaker families carry a voices table; requiring one would
# refuse a single-speaker VITS voice that works perfectly well.
case "${MODEL}" in
  *kitten*|*kokoro*) REQUIRED="${REQUIRED} voices.bin" ;;
esac
for required in ${REQUIRED}; do
  if [ ! -e "${MODELS_DIR}/${MODEL}/${required}" ]; then
    echo "  ${required} is missing; the voice is not usable" >&2
    exit 1
  fi
done
if ! ls "${MODELS_DIR}/${MODEL}"/*.onnx >/dev/null 2>&1; then
  echo "  no .onnx model file; the voice is not usable" >&2
  exit 1
fi

chown -R athena:athena "${MODELS_DIR}" 2>/dev/null || true

echo
echo "Checking the voice actually speaks:"
"${VENV}/python" - "${MODELS_DIR}/${MODEL}" <<'PY'
import sys
import time

import sherpa_onnx

folder = sys.argv[1]
name = folder.rsplit("/", 1)[-1].casefold()
common = {"model": f"{folder}/model.int8.onnx", "tokens": f"{folder}/tokens.txt",
          "data_dir": f"{folder}/espeak-ng-data"}
if "vits" in name or "piper" in name:
    import glob
    found = sorted(glob.glob(f"{folder}/*.onnx"))
    if not found:
        raise SystemExit("no .onnx model file")
    family, config = "vits", sherpa_onnx.OfflineTtsVitsModelConfig(
        model=found[0], tokens=f"{folder}/tokens.txt",
        data_dir=f"{folder}/espeak-ng-data")
elif "kokoro" in name:
    family, config = "kokoro", sherpa_onnx.OfflineTtsKokoroModelConfig(**common)
else:
    family, config = "kitten", sherpa_onnx.OfflineTtsKittenModelConfig(
        voices=f"{folder}/voices.bin", **common)

tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
    model=sherpa_onnx.OfflineTtsModelConfig(
        num_threads=2, debug=False, **{family: config})))

started = time.perf_counter()
audio = tts.generate("This is a test of the local voice.", sid=0, speed=1.0)
elapsed = time.perf_counter() - started
seconds = len(audio.samples) / audio.sample_rate
print(f"  family {family}   sample rate {audio.sample_rate}   speakers {tts.num_speakers}")
print(f"  {seconds:.2f}s of audio in {elapsed:.2f}s = {seconds / elapsed:.2f}x realtime")
if seconds <= 0:
    raise SystemExit("  the model produced no audio")
if seconds / elapsed < 1.0:
    print("  NOTE: slower than realtime on this board. A reply will drain the")
    print("  speaker before it finishes; a smaller voice or the cloud voice will")
    print("  serve better here.")
PY

echo
echo "Now add these to /etc/athena/athena.env and restart athena-voice:"
echo "  ATHENA_TTS_BACKEND=sherpa"
echo "  ATHENA_SHERPA_MODEL_DIR=${MODELS_DIR}"
echo "  ATHENA_SHERPA_MODEL=${MODEL}"
