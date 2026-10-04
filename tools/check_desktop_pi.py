"""Read-only desktop integration test; retrieve password via trusted SSH, never print it."""
import getpass
import os
from pathlib import Path
import paramiko
from paramiko.hostkeys import HostKeyEntry, InvalidHostKey
from athena.desktop import Client, probe


def main():
    host = os.environ.get('ATHENA_PI_HOST', '192.168.33.153')
    ssh = paramiko.SSHClient()
    for line in (Path.home() / '.ssh/known_hosts').read_text().splitlines():
        if not line.strip() or line.startswith('#'): continue
        try: entry = HostKeyEntry.from_line(line)
        except (InvalidHostKey, ValueError): continue
        if entry and entry.key:
            for hostname in entry.hostnames: ssh.get_host_keys().add(hostname, entry.key.get_name(), entry.key)
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    ssh.connect(host, username='root', password=os.environ.get('ATHENA_PI_PASSWORD') or getpass.getpass('Pi password: '),
                timeout=10, look_for_keys=False, allow_agent=False)
    try:
        settings = {}
        with ssh.open_sftp() as sftp:
            for filename in ('/etc/athena/athena.env', '/etc/athena/web.env'):
                with sftp.open(filename) as stream:
                    for raw in stream.read().decode().splitlines():
                        if not raw.strip() or raw.lstrip().startswith('#') or '=' not in raw: continue
                        name, value = raw.split('=', 1); settings[name.strip()] = value.strip().strip('\"\'')
        # Certificate verification is anchored to the already-trusted SSH host key.
        command = "/usr/bin/python3 -c \"import hashlib,ssl; cert=ssl.get_server_certificate(('127.0.0.1',8780)); print(hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest())\""
        _, out, err = ssh.exec_command(command); trusted = out.read().decode().strip()
        if out.channel.recv_exit_status(): raise RuntimeError('Could not verify the Pi certificate over trusted SSH.')
        address, fingerprint = probe(host, 5)
        if trusted != fingerprint: raise RuntimeError('The LAN certificate does not match the trusted Pi.')
        client = Client(address, fingerprint)
        password = settings.get('ATHENA_WEB_PASSWORD', '') if settings.get('ATHENA_WEB_AUTH', '').lower() != 'off' else ''
        bootstrap = client.login(password)
        assert client.csrf and client.cookie
        monitor = client.request('/api/monitor'); tunables = client.request('/api/settings')
        assert 'operations' in monitor and 'metrics' in monitor and 'agents' in monitor
        assert tunables['settings']
        from athena.desktop import discover
        found = discover(str(__import__('ipaddress').ip_network(host + '/24', strict=False)))
        assert (host, fingerprint) in found
        print('PASS: trusted certificate, dashboard login, CSRF/session, live tasks/agents/metrics, all settings, LAN discovery.')
        print('Voice service:', bootstrap.get('service'))
        print('Settings:', len(tunables['settings']), '| Recorded operations:', len(monitor['operations']))
        print('No speech, paid model request, reboot, or control action was triggered.')
    finally: ssh.close()


if __name__ == '__main__': main()
