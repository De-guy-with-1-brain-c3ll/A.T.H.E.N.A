"""Configure and start ATHENA's PC inbox without showing credentials."""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
KEY = ROOT / "orange_pi" / ".pc-transfer-key"
PID = ROOT / "orange_pi" / ".pc-inbox-process.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind")
    parser.add_argument("--pi")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.stop:
        if not PID.exists():
            print("No receiver process recorded.")
            return
        import psutil
        record = json.loads(PID.read_text())
        try:
            process = psutil.Process(record["pid"])
            if process.create_time() == record["created"] and "athena.pc_transfer" in process.cmdline():
                # Stop only this verified receiver's own driver/browser children,
                # so its dedicated profile isn't left locked after restart.
                for child in reversed(process.children(recursive=True)):
                    try: child.terminate()
                    except psutil.NoSuchProcess: pass
                process.terminate()
                print("PC receiver stopped.")
        except psutil.NoSuchProcess:
            print("PC receiver already stopped.")
        PID.unlink(missing_ok=True)
        return
    if not args.bind or not ipaddress.ip_address(args.bind).is_private:
        parser.error("Supply --bind with this computer's private LAN IPv4 address.")
    if PID.exists():
        parser.error("Receiver already recorded; use --stop before reconfiguring.")
    if not KEY.exists():
        KEY.write_text(secrets.token_hex(32), encoding="utf-8")
        if os.name != "nt": KEY.chmod(0o600)
    key = KEY.read_text().strip()
    if len(key) < 32: parser.error("Invalid transfer key; replace the local key file.")
    url = f"http://{args.bind}:8781/upload"
    if args.pi:
        import paramiko
        from paramiko.hostkeys import HostKeyEntry, InvalidHostKey
        client = paramiko.SSHClient()
        for line in (Path.home()/".ssh/known_hosts").read_text().splitlines():
            if not line.strip() or line.startswith("#"): continue
            try: entry = HostKeyEntry.from_line(line)
            except (InvalidHostKey, ValueError): continue
            if entry and entry.key:
                for host in entry.hostnames: client.get_host_keys().add(host, entry.key.get_name(), entry.key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(args.pi, username="root", password=os.environ.get("ATHENA_PI_PASSWORD") or getpass.getpass("Pi SSH password: "), timeout=8)
        try:
            with client.open_sftp() as sftp:
                target = "/etc/athena/athena.env"
                with sftp.open(target) as source: content = source.read().decode()
                lines = [line for line in content.splitlines() if not line.startswith(("ATHENA_PC_UPLOAD_URL=", "ATHENA_PC_TRANSFER_KEY="))]
                lines.extend([f"ATHENA_PC_UPLOAD_URL={url}", f"ATHENA_PC_TRANSFER_KEY={key}"])
                temporary = target + ".pc-transfer.tmp"
                with sftp.open(temporary, "w") as output: output.write("\n".join(lines) + "\n")
                sftp.chmod(temporary, 0o600)
                sftp.posix_rename(temporary, target)
            print("Pi transfer settings saved; restart ATHENA to load them.")
        finally: client.close()
    env = dict(os.environ, ATHENA_PC_TRANSFER_KEY=key)
    (ROOT / "logs").mkdir(exist_ok=True)
    with (ROOT/"logs"/"pc-inbox.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "athena.pc_transfer", "--bind", args.bind,
                                    "--inbox", str(ROOT/"ATHENA Inbox")], cwd=ROOT, env=env,
                                   stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    import psutil
    PID.write_text(json.dumps({"pid": process.pid, "created": psutil.Process(process.pid).create_time()}))
    print(f"Receiver launched on {args.bind}:8781. Inbox: {ROOT / 'ATHENA Inbox'}")


if __name__ == "__main__": main()
