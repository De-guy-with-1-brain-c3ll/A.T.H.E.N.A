"""Optional Linux VPN setup using verified upstream binaries for each CPU."""
import gzip
import hashlib
import json
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import urllib.request

ARCHITECTURES={'aarch64':'arm64','arm64':'arm64','armv7l':'armv7','armv8l':'armv7','x86_64':'amd64-compatible','amd64':'amd64-compatible'}

def download(url):
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'ATHENA-setup'}),timeout=90) as response:
        data=response.read(100_000_001)
    if len(data)>100_000_000: raise ValueError('VPN download is too large.')
    return data

def install(root):
    release=json.loads(download('https://api.github.com/repos/MetaCubeX/mihomo/releases/latest'))
    arch=ARCHITECTURES[platform.machine()]
    name=f'mihomo-linux-{arch}-{release["tag_name"]}.gz'
    asset=next(item for item in release['assets'] if item['name']==name)
    digest=asset.get('digest','')
    if not digest.startswith('sha256:'): raise ValueError('Upstream did not provide a binary checksum.')
    packed=download(asset['browser_download_url'])
    if hashlib.sha256(packed).hexdigest()!=digest[7:]: raise ValueError('VPN checksum mismatch; installation refused.')
    target=Path('/usr/local/bin/mihomo'); target.write_bytes(gzip.decompress(packed)); target.chmod(0o755)
    for directory in ('/etc/athena-vpn','/var/lib/athena-vpn','/usr/local/lib/athena-vpn'):
        Path(directory).mkdir(parents=True,exist_ok=True)
    for source in ('orange_pi/pi/vpn_control.py','src/athena/vpn_config.py'):
        shutil.copy2(root/source,Path('/usr/local/lib/athena-vpn')/Path(source).name)
    shutil.copy2(root/'orange_pi/pi/athena-vpn-control','/usr/local/bin/athena-vpn-control')
    Path('/usr/local/bin/athena-vpn-control').chmod(0o755)
    shutil.copy2(root/'orange_pi/pi/vpn_server.py','/opt/athena/vpn_server.py')
    shutil.copy2(root/'orange_pi/config/athena-vpn.service','/etc/systemd/system/athena-vpn.service')
    shutil.copy2(root/'orange_pi/systemd/athena-vpn-control.service','/etc/systemd/system/athena-vpn-control.service')
    token=Path('/etc/athena-vpn/token')
    if not token.exists(): token.write_text(secrets.token_hex(32)); token.chmod(0o600)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','athena-vpn-control'],check=True)
    source=input('Path to your exported Clash/Mihomo YAML (Enter to import later): ').strip()
    if source:
        # The controller deletes its input, so give it a private temporary copy.
        staged=Path('/etc/athena-vpn/setup-import.yaml')
        staged.write_bytes(Path(source).expanduser().read_bytes()); staged.chmod(0o600)
        subprocess.run(['/usr/local/bin/athena-vpn-control','apply',str(staged)],check=True)
        subprocess.run(['systemctl','disable','--now','athena-vpn'],check=True)
    print('VPN installed. It is stopped; ask Athena to start it when ready.')
