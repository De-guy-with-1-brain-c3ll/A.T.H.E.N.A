"""Guided, architecture-independent Linux installation, with an offline check mode."""
import argparse
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from athena.installation import decode_pairing,pairing_code

def run(*args): subprocess.run(list(map(str,args)),check=True)

def write(path,text,mode=0o600):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.setup.tmp'); temporary.write_text(text,encoding='utf-8'); temporary.chmod(mode); temporary.replace(path)

def ask(label,secret=False,optional=False):
    while True:
        value=(getpass.getpass(label+': ') if secret else input(label+': ')).strip()
        if not value and optional:return ''
        if value and not any(c in value for c in ('\n','\r','\0')):return value
        print('Please enter a value.')

def configure(existing=False):
    print('\nATHENA setup — your keys stay on this device.\nVoice uses DeepSeek and DashScope accounts; Edge speech is free.\n')
    values={'ATHENA_DATA_DIR':'/opt/athena/data','ATHENA_DATABASE_PATH':'/opt/athena/data/athena.db',
            'ATHENA_TTS_BACKEND':'edge','ATHENA_EDGE_VOICE':'en-US-AvaNeural','ATHENA_STT_BACKEND':'qwen',
            'ATHENA_AUDIO_STATUS_PATH':'/run/athena/audio-status.json','ATHENA_REMOTE_AUDIO':'0',
            'ATHENA_CJ_SCHEDULE':'0','ATHENA_WEB_TLS_CERT':'/etc/athena/dashboard.crt','ATHENA_WEB_TLS_KEY':'/etc/athena/dashboard.key'}
    for label,name,secret,optional in [('DeepSeek API key','DEEPSEEK_API_KEY',True,False),
        ('DashScope speech API key','DASHSCOPE_API_KEY',True,False),('Your timezone (example Asia/Shanghai)','ATHENA_TIMEZONE',False,False),
        ('Microsoft Teams app ID (Enter to skip)','MICROSOFT_CLIENT_ID',False,True),
        ('Tavily search key (Enter to skip)','TAVILY_API_KEY',True,True),
        ('Feishu app ID (Enter to skip)','FEISHU_APP_ID',False,True),('Feishu app secret (Enter to skip)','FEISHU_APP_SECRET',True,True)]:
        values[name]=ask(label,secret,optional)
    password=ask('Choose a dashboard password',True)
    code=ask('PC pairing code, hidden while pasting (Enter to pair later)',True,True)
    if code:
        pc=decode_pairing(code,'pc'); values.update(ATHENA_PC_UPLOAD_URL=pc['url'],ATHENA_PC_TRANSFER_KEY=pc['key'])
        if input('Use the paired PC microphone/speakers instead of local devices? [y/N] ').strip().lower()=='y':values['ATHENA_REMOTE_AUDIO']='1'
    return values,password

def install():
    if sys.version_info<(3,11):raise RuntimeError('Use a current Linux image with Python 3.11 or newer (Debian 12+, Ubuntu 24.04+, current Fedora/Arch).')
    if os.geteuid()!=0:raise RuntimeError('Run Install ATHENA with sudo.')
    configure_again=not Path('/etc/athena/athena.env').exists()
    if configure_again: values,password=configure()
    elif input('Keep your existing keys, settings and conversations? [Y/n] ').strip().lower()!='n': values=password=None
    else: values,password=configure()
    version=(ROOT/'orange_pi/VERSION').read_text().strip(); base=Path('/opt/athena'); release=base/'releases'/f'{version}-setup-{secrets.token_hex(3)}'
    # Build the complete new runtime before changing the running installation.
    release.mkdir(parents=True); shutil.copytree(ROOT/'src',release/'src'); shutil.copy2(ROOT/'pyproject.toml',release)
    shutil.copytree(ROOT/'orange_pi',release/'orange_pi',ignore=shutil.ignore_patterns('update_feed','.*'))
    run(sys.executable,'-m','venv',release/'.venv')
    run(release/'.venv/bin/python','-m','pip','install','--upgrade','pip')
    run(release/'.venv/bin/python','-m','pip','install',release)
    if subprocess.run(['id','athena'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        run('useradd','--system','--home-dir','/opt/athena','--shell','/usr/sbin/nologin','athena')
    if subprocess.run(['getent','group','audio'],stdout=subprocess.DEVNULL).returncode:run('groupadd','--system','audio')
    run('usermod','-aG','audio','athena')
    run('install','-d','-o','athena','-g','athena','-m','0750',base/'data')
    Path('/etc/athena').mkdir(parents=True,exist_ok=True)
    if values is not None:
        write('/etc/athena/athena.env',''.join(f'{k}={json.dumps(v,ensure_ascii=False)}\n' for k,v in values.items()))
        write('/etc/athena/web.env',f'ATHENA_WEB_PASSWORD={json.dumps(password,ensure_ascii=False)}\nATHENA_WEB_SECRET={secrets.token_urlsafe(48)}\n')
    cert=Path('/etc/athena/dashboard.crt'); key=Path('/etc/athena/dashboard.key')
    if not cert.exists() or not key.exists():
        run('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','3650','-subj','/CN=ATHENA','-keyout',key,'-out',cert)
        key.chmod(0o640); run('chown','root:athena',key)
    for name in ('athena-voice.service','athena-web.service','athena-feishu.service'):
        shutil.copy2(ROOT/'orange_pi/systemd'/name,Path('/etc/systemd/system')/name)
        memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:')))//1024
        write(Path('/etc/systemd/system')/(name+'.d')/'memory.conf',f'[Service]\nMemoryHigh={min(850,max(192,int(memory*.45)))}M\nMemoryMax={min(1200,max(256,int(memory*.65)))}M\n',0o644)
    sudoers=ROOT/'orange_pi/config/athena-web-control.sudoers'
    run('visudo','-cf',sudoers); shutil.copy2(sudoers,'/etc/sudoers.d/athena-web-control'); Path('/etc/sudoers.d/athena-web-control').chmod(0o440)
    current=base/'current'; previous=current.resolve() if current.exists() else None
    link=base/'next'; link.unlink(missing_ok=True); link.symlink_to(release,target_is_directory=True); link.replace(current)
    try:
        run('systemctl','daemon-reload'); run('systemctl','enable','--now','athena-web')
        run('systemctl','restart','athena-web')
    except Exception:
        if previous:
            link.symlink_to(previous,target_is_directory=True); link.replace(current); subprocess.run(['systemctl','restart','athena-web'])
        raise
    print('\nInstalled successfully. Voice has not been started or tested.\n')
    if input('Install optional VPN controls (requires your own VPN account)? [y/N] ').strip().lower()=='y':
        try:
            from vpn import install as install_vpn
            install_vpn(ROOT)
        except Exception as error: print('Athena is installed, but optional VPN setup failed:',str(error))
    addresses=[x for x in subprocess.check_output(['hostname','-I'],text=True).split() if ':' not in x and ipaddress.ip_address(x).is_private]
    host=addresses[0] if addresses else '127.0.0.1'
    import ssl
    fingerprint=hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
    print('Open https://'+host+':8780 to sign in, configure integrations, and start listening.')
    if password:
        code=pairing_code({'role':'linux','host':host,'fingerprint':fingerprint,'password':password})
        print('\nPaste this private device code into ATHENA Companion on Windows:\n'+code)
    print('\nYour saved files and conversations are in /opt/athena/data. Re-running setup keeps them.')
    if input('Sign in to Microsoft Teams now? [y/N] ').strip().lower()=='y':
        subprocess.run(['systemd-run','--wait','--pipe','--collect','--property=User=athena','--property=EnvironmentFile=/etc/athena/athena.env','--property=WorkingDirectory=/opt/athena/data','/opt/athena/current/.venv/bin/athena-teams-login'])

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--check',action='store_true'); args=parser.parse_args()
    if args.check:
        for path in ('pyproject.toml','src/athena/main.py','src/athena/windows_app.py','orange_pi/systemd/athena-voice.service'):
            assert (ROOT/path).is_file(),path
        print(json.dumps({'package_ok':True,'python':platform.python_version(),'architecture':platform.machine(),'systemd_required':True,'speaker_used':False})); return
    install()

if __name__=='__main__':main()
