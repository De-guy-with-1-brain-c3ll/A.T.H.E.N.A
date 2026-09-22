#!/usr/bin/env bash
# Set up ATHENA inside WSL so it can be developed and tested without the Pi.
#
#   bash tools/setup_wsl.sh
#
# Safe to re-run. It installs the system packages ATHENA needs, builds the
# virtual environment, verifies the coding sandbox works, and runs the tests.
set -uo pipefail

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

if [ "$(uname -s)" != "Linux" ]; then
  fail "Run this inside WSL, not on Windows. Open your WSL terminal and try again."
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT" || fail "Could not enter $PROJECT_ROOT"
echo "Project: $PROJECT_ROOT"
case "$PROJECT_ROOT" in
  /mnt/*) echo "Note: this is on the Windows drive. It works, but file access is slow."
          echo "      For faster tests, copy the project into your Linux home first." ;;
esac

say "1. System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3 python3-venv python3-pip \
  portaudio19-dev libasound2-dev \
  ffmpeg bubblewrap git
echo "Installed."

say "2. Python environment"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || fail "Could not create .venv"
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e . || fail "pip install failed"
.venv/bin/python -c "import athena, sys; print('ATHENA importable on', sys.version.split()[0])"

say "3. Coding sandbox (bubblewrap)"
# The Pi uses bubblewrap, so testing it here exercises the same code path.
if bwrap --unshare-all --ro-bind /usr /usr --dev /dev --proc /proc \
        /usr/bin/python3 -c "print('bubblewrap works')" 2>/dev/null; then
  echo "Bubblewrap can sandbox generated code here."
else
  echo "Bubblewrap could not start. On WSL1 enable it, or use WSL2."
  echo "The coding tool will report the sandbox as unavailable, which is safe."
fi

say "4. Tests"
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -5

say "5. Try it"
cat <<'EOF'
  # Speak an alarm through the whole voice path and watch it really fire
  .venv/bin/python -m athena.dev.harness alarms

  # See exactly how the voice gate reacts, frame by frame
  .venv/bin/python -m athena.dev.harness vad

  # Type to ATHENA offline; alarms still fire while you talk
  .venv/bin/python -m athena.dev.harness chat

  # Real text chat with the real model (needs DEEPSEEK_API_KEY in .env)
  .venv/bin/python -m athena.text

  # Just the tests
  .venv/bin/python -m unittest discover -s tests
EOF
echo
echo "Everything runs against a throwaway data directory: your real alarms,"
echo "memory and settings are never touched."
