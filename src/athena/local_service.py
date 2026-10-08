"""Own-process management for the Windows installation; no arbitrary commands."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import psutil
from athena.installation import home,environment
from athena.alerts import _FileLock

LOCK=threading.RLock()
ALLOWED={'voice','web','inbox','feishu','teams'}

def command(worker):
    if worker not in ALLOWED: raise ValueError('Unknown ATHENA component.')
    if getattr(sys,'frozen',False): return [sys.executable,'--worker',worker]
    return [sys.executable,'-m','athena.windows_app','--worker',worker]

def process(worker):
    try:
        record=json.loads((home()/f'{worker}.pid.json').read_text())
        p=psutil.Process(record['pid'])
        args=p.cmdline()
        if abs(p.create_time()-record['created'])<.01 and args==record['command'] and args==command(worker): return p
    except (OSError,ValueError,KeyError,psutil.Error): pass
    return None

def status(worker='voice'): return 'active' if process(worker) else 'inactive'

def stop(worker):
    if worker not in ALLOWED: raise ValueError('Unknown ATHENA component.')
    with LOCK,_FileLock(home()/f'{worker}.service.lock'):
        p=process(worker)
        if p:
            for child in reversed(p.children(recursive=True)):
                if child.pid==os.getpid(): continue
                try: child.terminate()
                except psutil.Error: pass
            p.terminate()
            try: p.wait(5)
            except psutil.TimeoutExpired: p.kill()
        (home()/f'{worker}.pid.json').unlink(missing_ok=True)

def start(worker):
    if worker not in ALLOWED: raise ValueError('Unknown ATHENA component.')
    with LOCK,_FileLock(home()/f'{worker}.service.lock'):
        if process(worker): return
        root=home(); (root/'logs').mkdir(parents=True,exist_ok=True)
        env=os.environ.copy(); env.update(environment())
        cmd=command(worker)
        with (root/'logs'/f'{worker}.log').open('ab') as output:
            p=subprocess.Popen(cmd,env=env,cwd=root,stdout=output,stderr=output,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (root/f'{worker}.pid.json').write_text(json.dumps({'pid':p.pid,'created':psutil.Process(p.pid).create_time(),'command':cmd}))
        # Surface immediate failures instead of reporting a dead worker as started.
        try:
            code=p.wait(timeout=.35)
            raise RuntimeError(f'{worker} could not start (exit {code}). Open logs/{worker}.log for details.')
        except subprocess.TimeoutExpired: pass

def control(action,worker='voice'):
    if action not in ('start','stop','restart'): raise ValueError('Unknown service action.')
    if action in ('stop','restart'): stop(worker)
    if action in ('start','restart'): start(worker)
