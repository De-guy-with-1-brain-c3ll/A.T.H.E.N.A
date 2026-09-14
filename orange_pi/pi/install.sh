#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi

architecture="$(uname -m)"
if [[ "${architecture}" != "aarch64" && "${architecture}" != "arm64" ]]; then
  echo "Warning: expected a 64-bit ARM OS, detected ${architecture}." >&2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
orange_dir="$(cd -- "${script_dir}/.." && pwd)"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev build-essential \
  portaudio19-dev libasound2-dev bubblewrap bluez-alsa-utils ca-certificates sudo ffmpeg

if ! id athena >/dev/null 2>&1; then
  useradd --system --home-dir /opt/athena --shell /usr/sbin/nologin --groups audio athena
else
  usermod -a -G audio athena
fi

install -d -o root -g root -m 0755 /opt/athena /opt/athena/releases
install -d -o athena -g athena -m 0750 /opt/athena/data
install -d -o root -g root -m 0755 /opt/athena/cache /etc/athena
install -o root -g root -m 0755 "${script_dir}/update_client.py" /opt/athena/update_client.py
install -o root -g root -m 0755 "${script_dir}/rollback.py" /usr/local/bin/athena-rollback

if [[ ! -f /etc/athena/athena.env ]]; then
  install -o root -g athena -m 0640 "${orange_dir}/config/athena.env.example" /etc/athena/athena.env
fi
if [[ ! -f /etc/athena/update.env ]]; then
  install -o root -g root -m 0600 "${orange_dir}/config/update.env.example" /etc/athena/update.env
fi
if [[ ! -f /etc/athena/web.env ]]; then
  web_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  web_secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  temporary_web_env="$(mktemp)"
  printf 'ATHENA_WEB_PASSWORD=%s\nATHENA_WEB_SECRET=%s\n' \
    "${web_password}" "${web_secret}" >"${temporary_web_env}"
  install -o root -g athena -m 0640 "${temporary_web_env}" /etc/athena/web.env
  rm -f -- "${temporary_web_env}"
  echo "ATHENA dashboard password: ${web_password}"
fi

for unit in athena-feishu.service athena-voice.service athena-web.service athena-update.service athena-update.timer; do
  install -o root -g root -m 0644 "${orange_dir}/systemd/${unit}" "/etc/systemd/system/${unit}"
done
install -o root -g root -m 0440 "${orange_dir}/config/athena-web-control.sudoers" \
  /etc/sudoers.d/athena-web-control
visudo -cf /etc/sudoers.d/athena-web-control >/dev/null
install -d -o root -g root -m 0755 /etc/systemd/system/bluealsa.service.d
install -o root -g root -m 0644 "${orange_dir}/systemd/bluealsa.service.d/override.conf" \
  /etc/systemd/system/bluealsa.service.d/override.conf
install -d -o root -g root -m 0755 /etc/systemd/system/athena-voice.service.d
install -o root -g root -m 0644 "${orange_dir}/systemd/athena-voice.service.d/usb-debug.conf" \
  /etc/systemd/system/athena-voice.service.d/usb-debug.conf
systemctl daemon-reload
systemctl enable athena-web.service

echo
echo "Orange Pi support is installed."
echo "1. Edit /etc/athena/athena.env"
echo "2. Edit /etc/athena/update.env"
echo "3. Publish and serve a release on the Windows computer"
echo "4. Run: sudo systemctl start athena-update.service"
echo "5. Enable one interface; do not enable both voice and Feishu"
echo "6. Open http://PI_ADDRESS:8780 after the first update installs"
