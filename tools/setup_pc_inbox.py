"""Configure and start ATHENA's PC inbox without showing credentials."""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
KEY = ROOT / "orange_pi" / ".pc-transfer-key"
PID = ROOT / "orange_pi" / ".pc-inbox-process.json"


def firewall_rule_missing(port: int = 8781) -> bool | None:
    """Whether Windows is letting inbound LAN traffic into `port`.

    Returns None when the answer cannot be established (not Windows, no
    permission to read the rules), so the caller only warns when it is sure.
    A receiver that binds successfully but is unreachable from the Pi looks
    exactly like a dead receiver from the Pi's side, and the firewall is the
    usual reason — so it is worth checking before anything is deployed.
    """
    if os.name != "nt":
        return None
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        pass
    script = (
        "$r = Get-NetFirewallRule -Direction Inbound -Enabled True -Action Allow "
        "| Where-Object { $_.DisplayName -like '*ATHENA*' } "
        "| Get-NetFirewallPortFilter | Where-Object { $_.LocalPort -eq "
        f"{port} }}; if ($r) {{ 'OPEN' }} else {{ 'MISSING' }}"
    )
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                              capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError):
        return None
    answer = done.stdout.strip()
    if answer == "OPEN":
        return False
    if answer == "MISSING":
        return True
    return None


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
    # A launcher that reports success for a process that died on startup is worse
    # than one that fails: the Pi then silently gets no audio and no transfers,
    # and the cause is invisible because the traceback is buried in the log.
    # So confirm the socket is actually accepting before claiming it launched.
    for _ in range(40):
        if process.poll() is not None:
            break
        try:
            with socket.create_connection((args.bind, 8781), timeout=1):
                print(f"Receiver launched on {args.bind}:8781 and is accepting connections. "
                      f"Inbox: {ROOT / 'ATHENA Inbox'}")
                return
        except OSError:
            time.sleep(0.25)
    # Checked before launching: a receiver that binds fine but is firewalled
    # is unreachable from the Pi, which is indistinguishable from a dead one
    # when you are standing at the Pi.
    if firewall_rule_missing(8781):
        print(f"WARNING: Windows has no inbound firewall rule for port 8781, so the Pi\n"
              f"cannot reach this receiver even though it starts. Run this in an\n"
              f"Administrator PowerShell, then start the receiver again:\n\n"
              f'  New-NetFirewallRule -DisplayName "ATHENA PC Receiver 8781" '
              f"-Direction Inbound -Action Allow -Protocol TCP -LocalPort 8781 -Profile Any\n",
              file=sys.stderr)
    log_path = ROOT / "logs" / "pc-inbox.log"
    detail = ""
    if log_path.is_file():
        detail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:]
        detail = f" Last log line: {detail[0]}" if detail else ""
    PID.unlink(missing_ok=True)
    raise SystemExit(
        f"Receiver did not start on {args.bind}:8781 "
        f"(process exit code {process.poll()}).{detail}\n"
        f"Check the bind address matches this computer's LAN IPv4, and that "
        f"logs/pc-inbox.log has the full traceback.")


if __name__ == "__main__": main()
