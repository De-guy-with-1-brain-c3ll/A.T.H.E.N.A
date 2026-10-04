"""Install narrowly scoped service controls; validate before replacing sudoers."""
import argparse
import getpass
import os
from pathlib import Path
import time
import paramiko
from paramiko.hostkeys import HostKeyEntry, InvalidHostKey


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--host', default='192.168.33.153')
    args = parser.parse_args(); client = paramiko.SSHClient()
    for line in (Path.home() / '.ssh/known_hosts').read_text().splitlines():
        if not line.strip() or line.startswith('#'): continue
        try: entry = HostKeyEntry.from_line(line)
        except (InvalidHostKey, ValueError): continue
        if entry and entry.key:
            for host in entry.hostnames: client.get_host_keys().add(host, entry.key.get_name(), entry.key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(args.host, username='root', password=os.environ.get('ATHENA_PI_PASSWORD') or getpass.getpass('Pi password: '),
                   timeout=10, look_for_keys=False, allow_agent=False)
    try:
        remote = f'/tmp/athena-desktop-sudoers-{int(time.time())}'
        with client.open_sftp() as sftp:
            sftp.put(str(Path(__file__).resolve().parents[1] / 'orange_pi/config/athena-web-control.sudoers'), remote)
            sftp.chmod(remote, 0o600)
        command = f'/usr/sbin/visudo -cf {remote} && install -o root -g root -m 0440 {remote} /etc/sudoers.d/athena-web-control'
        _, out, err = client.exec_command(command)
        print(out.read().decode(), end=''); errors = err.read().decode()
        status = out.channel.recv_exit_status()
        if status: raise RuntimeError(errors)
        print('Desktop service permissions installed. No services restarted and no reboot performed.')
    finally: client.close()


if __name__ == '__main__': main()
