"""Install verified official ARM64 Mihomo and ATHENA's narrow controller over SSH."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import sys
import urllib.request
import getpass
import paramiko

ROOT = Path(__file__).resolve().parents[1]

def connect(host, password):
    client = paramiko.SSHClient()
    from paramiko.hostkeys import HostKeyEntry, InvalidHostKey
    for line in (Path.home()/".ssh/known_hosts").read_text().splitlines():
        try: entry = HostKeyEntry.from_line(line)
        except (InvalidHostKey, ValueError): continue
        if entry and entry.key:
            for name in entry.hostnames: client.get_host_keys().add(name, entry.key.get_name(), entry.key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(host, username="root", password=password, timeout=30, banner_timeout=30,
                   look_for_keys=False, allow_agent=False)
    return client

def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "ATHENA-installer"})
    with urllib.request.urlopen(request, timeout=60) as response: return response.read(100_000_000)

def main():
    release = json.loads(download("https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"))
    name = "mihomo-linux-arm64-" + release["tag_name"] + ".gz"
    asset = next(item for item in release["assets"] if item["name"] == name)
    expected = asset.get("digest", "")
    if not expected.startswith("sha256:"):
        raise RuntimeError("Official release lacks a SHA256 digest; refusing unverified installation.")
    packed = download(asset["browser_download_url"])
    if hashlib.sha256(packed).hexdigest() != expected[7:]: raise RuntimeError("Binary checksum mismatch")
    binary = gzip.decompress(packed)
    host = (ROOT/"orange_pi/pi-address.txt").read_text().strip()
    client = connect(host, os.environ.get("ATHENA_PI_PASSWORD") or getpass.getpass("Pi password: "))
    stage = "/tmp/athena-vpn-install-" + secrets.token_hex(8)
    try:
        sftp = client.open_sftp(); sftp.mkdir(stage)
        with sftp.open(stage+"/mihomo", "wb") as output: output.write(binary)
        sources = {"vpn_control.py": ROOT/"orange_pi/pi/vpn_control.py",
                   "vpn_config.py": ROOT/"src/athena/vpn_config.py",
                   "athena-vpn.service": ROOT/"orange_pi/config/athena-vpn.service",
                   "athena-vpn-control": ROOT/"orange_pi/pi/athena-vpn-control",
                   "athena-vpn.sudoers": ROOT/"orange_pi/config/athena-vpn.sudoers"}
        for name, source in sources.items(): sftp.put(str(source), stage+"/"+name)
        # All source paths are installer-generated, not imported config contents.
        command = f"""set -eu
test "$(uname -m)" = aarch64
test -c /dev/net/tun
apt-get install -y python3-yaml
install -d -m 700 /etc/athena-vpn /var/lib/athena-vpn
install -d -m 755 /usr/local/lib/athena-vpn
install -m 755 {stage}/mihomo /usr/local/bin/mihomo
install -m 644 {stage}/vpn_control.py {stage}/vpn_config.py /usr/local/lib/athena-vpn/
install -m 755 {stage}/athena-vpn-control /usr/local/bin/athena-vpn-control
install -m 644 {stage}/athena-vpn.service /etc/systemd/system/athena-vpn.service
visudo -cf {stage}/athena-vpn.sudoers
install -m 440 {stage}/athena-vpn.sudoers /etc/sudoers.d/athena-vpn
if ! test -f /etc/athena-vpn/token; then python3 -c 'import secrets,pathlib; p=pathlib.Path("/etc/athena-vpn/token"); p.write_text(secrets.token_hex(32)); p.chmod(0o600)'; fi
systemctl daemon-reload
/usr/local/bin/mihomo -v
/usr/local/bin/athena-vpn-control status
"""
        _, stdout, stderr = client.exec_command(command, timeout=180)
        print(stdout.read().decode(errors="replace"))
        if stdout.channel.recv_exit_status(): raise RuntimeError(stderr.read().decode(errors="replace")[:1000])
        print("Verified Mihomo installed. VPN stays stopped until you import a config.")
    finally: client.close()

if __name__ == "__main__": main()
