#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
trap 'printf "\nSetup stopped at line %s. Your previous installation was kept where possible.\n" "$LINENO" >&2' ERR
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
if [[ "${1:-}" == --check ]]; then
  python3 packaging/linux/setup.py --check
  exit
fi
if [[ $EUID -ne 0 ]]; then exec sudo bash packaging/linux/install.sh "$@"; fi
command -v systemctl >/dev/null || { echo 'This installer needs a Linux OS with systemd (Debian, Ubuntu, Armbian, Raspberry Pi OS, Fedora or Arch).'; exit 1; }
case "$(uname -m)" in aarch64|arm64|armv7l|armv8l|x86_64|amd64) ;; *) echo 'Use a 64-bit ARM, ARMv7, or x86-64 Linux OS.'; exit 1;; esac
if command -v apt-get >/dev/null; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip python3-dev python3-yaml build-essential portaudio19-dev libasound2-dev bubblewrap ffmpeg openssl ca-certificates sudo
elif command -v dnf >/dev/null; then
  dnf install -y python3 python3-pip python3-devel python3-pyyaml gcc gcc-c++ portaudio-devel alsa-lib-devel bubblewrap ffmpeg-free openssl ca-certificates sudo
elif command -v pacman >/dev/null; then
  pacman -Syu --noconfirm --needed python python-pip python-yaml base-devel portaudio alsa-lib bubblewrap ffmpeg openssl ca-certificates sudo
else
  echo 'Supported package managers: apt (Debian/Ubuntu), dnf (Fedora), pacman (Arch).'; exit 1
fi
python3 packaging/linux/setup.py "$@"
