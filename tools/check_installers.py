"""Silent installer smoke tests in isolated directories. Never starts voice."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import ssl
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]

def request(url,body=None,headers=None):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    with opener.open(urllib.request.Request(url,data=body,headers=headers or {}),timeout=5) as response:return json.loads(response.read())

def wait(url,process):
    for attempt in range(60):
        if process.poll() is not None:raise RuntimeError('Packaged worker exited before becoming ready.')
        try:return request(url)
        except (OSError,urllib.error.URLError):time.sleep(.2)
    raise RuntimeError('Packaged worker did not become ready.')

def test(variant,port):
    target=ROOT/'outputs/installer-smoke'/variant
    target.mkdir(parents=True,exist_ok=True)
    install=target/'application'; profile=target/'profile';profile.mkdir(exist_ok=True)
    env=os.environ.copy();env['ATHENA_APP_HOME']=str(profile)
    config={'DEEPSEEK_API_KEY':'installer-test-not-a-real-key','DASHSCOPE_API_KEY':'installer-test-not-a-real-key',
        'ATHENA_PC_TRANSFER_KEY':secrets.token_urlsafe(48),'ATHENA_WEB_SECRET':secrets.token_urlsafe(48),
        'ATHENA_LOCAL_CONTROL_TOKEN':secrets.token_urlsafe(48),'ATHENA_WEB_PASSWORD':'installer-test-only',
        'ATHENA_WEB_PORT':str(port+1),'receiver_bind':'127.0.0.1','inbox_port':port}
    (profile/'configuration.json').write_text(json.dumps(config))
    subprocess.run([str(ROOT/f'dist/installers/ATHENA-{variant}-Windows-Setup.exe'),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS','/TASKS=',f'/DIR={install}',f'/LOG={target}/setup.log'],check=True)
    executable=install/f'ATHENA {variant}.exe'
    report=target/'self-test.json'
    subprocess.run([str(executable),'--self-test','--report',str(report)],env=env,check=True,timeout=45)
    assert json.loads(report.read_text())['ok']
    processes=[]
    try:
        receiver=subprocess.Popen([str(executable),'--worker','inbox'],env=env);processes.append(receiver)
        nonce=uuid4().hex
        wait(f'http://127.0.0.1:{port}/identify?nonce={nonce}',receiver)
        from athena.pc_transfer import signature
        for size in (0,1,65535,65536,1048576):
            payload=os.urandom(size); digest=hashlib.sha256(payload).hexdigest();stamp=str(int(time.time()));nonce=uuid4().hex;name=f'sample-{size}.bin'
            headers={'X-Athena-Time':stamp,'X-Athena-Nonce':nonce,'X-Athena-Name':name,'X-Athena-SHA256':digest,
                'X-Athena-Signature':signature(config['ATHENA_PC_TRANSFER_KEY'],stamp,nonce,name,digest)}
            receipt=request(f'http://127.0.0.1:{port}/upload',payload,headers)
            assert receipt['bytes']==size and receipt['sha256']==digest
            assert (profile/'Inbox'/receipt['filename']).read_bytes()==payload
        dashboard=subprocess.Popen([str(executable),'--worker','web'],env=env);processes.append(dashboard)
        health=wait(f'https://127.0.0.1:{port+1}/health',dashboard);assert health['app']=='athena'
        login=request(f'https://127.0.0.1:{port+1}/api/login',json.dumps({'password':config['ATHENA_WEB_PASSWORD']}).encode(),{'Content-Type':'application/json'})
        assert login['ok'] and login['csrf']
        print(variant,'installed runtime, five hashed transfers, HTTPS and login passed; no microphone/speaker used.')
    finally:
        for process in reversed(processes):
            process.terminate()
            try:process.wait(10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        subprocess.run([str(install/'unins000.exe'),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART'],env=env,check=True)
    assert (profile/'configuration.json').is_file()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--variant',choices=['Standalone','Companion']);args=parser.parse_args()
    for index,variant in enumerate([args.variant] if args.variant else ['Standalone','Companion']):test(variant,18881+index*10)

if __name__=='__main__':main()
