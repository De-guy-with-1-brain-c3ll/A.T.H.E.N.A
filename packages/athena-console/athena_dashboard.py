"""Live ANSI terminal dashboard. Standard library only; no speech dependencies."""
from collections import deque
import ctypes
import json
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from uuid import uuid4

RESET='\033[0m'
CYAN='\033[96m'
BLUE='\033[94m'
GREEN='\033[92m'
YELLOW='\033[93m'
RED='\033[91m'
DIM='\033[90m'


def safe(text):
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', str(text)).replace('\r','')


def enable_terminal():
    if os.name != 'nt' or not sys.stdin.isatty() or not sys.stdout.isatty(): return False
    kernel=ctypes.windll.kernel32
    kernel.GetStdHandle.restype=ctypes.c_void_p
    handle=kernel.GetStdHandle(-11)
    mode=ctypes.c_ulong()
    return bool(kernel.GetConsoleMode(handle,ctypes.byref(mode)) and kernel.SetConsoleMode(handle,mode.value|4))


def render(snapshot, draft='', notices=(), width=90, height=32, scroll=0, busy=False):
    width=max(45,width-1); height=max(20,height)
    voice=snapshot.get('voice',{})
    turns=snapshot.get('active_turns',[])
    state=voice.get('state','off')
    if state!='speaking' and turns: state=turns[-1].get('state','thinking')
    elif state=='off' and busy: state='thinking'
    online=snapshot.get('core')=='online'
    rows=[CYAN+r'     _   _____ _   _ _____ _   _    _'+RESET,
          BLUE+r'    / \ |_   _| |_| | ____| \ | |  / \ '+RESET,
          CYAN+r'   / _ \  | | |  _  |  _| |  \| | / _ \ '+RESET,
          BLUE+r'  /_/ \_\ |_| |_| |_|_____|_| \_/_/ \_\ '+RESET]
    rows.append((GREEN if online else RED)+' CORE '+('ONLINE' if online else 'OFFLINE')+RESET+
        ' | GATEWAY '+('ON' if snapshot.get('gateway') else 'OFF')+' | '+YELLOW+state.upper()+RESET)
    rows.append(DIM+'-'*width+RESET)
    rows.append(CYAN+' LIVE TRANSCRIPT'+RESET+'  '+safe(voice.get('heard') or voice.get('transcript') or '(waiting for speech)')[:width-20])
    rows.append(DIM+' Voice: '+safe(snapshot.get('voice_name','off'))[:width-12]+RESET)
    rows.append(CYAN+' TASKS'+RESET+'  (running work continues when you exit)')
    tasks=snapshot.get('tasks',[])
    active=[t for t in tasks if t['state'] in {'running','queued','verifying','cancelling'}]
    chosen=(active+[t for t in tasks if t not in active and t['state']!='awaiting_approval'])[:3]
    for task in chosen:
        status=task.get('activity') or {'reply_received':'finished: unchecked','unconfirmed':'status unknown',
            'awaiting_approval':'needs review'}.get(task['state'],task['state'].replace('_',' '))
        color=RED if task['state'] in {'failed','unconfirmed'} else GREEN if task['state'] in {'verified','reply_received'} else YELLOW
        rows.append(' '+safe(task['id'])+' '+color+status[:18].ljust(18)+RESET+' '+safe(task['objective'])[:max(8,width-34)])
    while len(chosen)<3:
        rows.append(DIM+'  --'+RESET); chosen.append({})
    approvals=snapshot.get('approvals',[])
    rows.append(GREEN+' Routine local tasks: automatic'+RESET+(YELLOW+f' | {len(approvals)} pending review(s)'+RESET if approvals else ''))
    rows.append(DIM+'-'*width+RESET)
    rows.append(CYAN+' CONVERSATION'+RESET+'  [PgUp/PgDn scroll]')
    lines=[]
    for item in snapshot.get('conversation',[]):
        role='YOU' if item.get('role')=='user' else 'ATHENA'
        prefix=role+(' [voice]' if item.get('mode')=='voice' else '')+': '
        for paragraph in safe(item.get('text','')).splitlines():
            wrapped=textwrap.wrap(prefix+paragraph,width-2) or [prefix]
            lines.extend((GREEN if role=='YOU' else CYAN)+line+RESET for line in wrapped)
            prefix='  '
    for turn in turns:
        if turn.get('text'):
            lines.extend(CYAN+line+RESET for line in textwrap.wrap('ATHENA (live): '+safe(turn['text']),width-2))
    for notice in notices:
        lines.extend(YELLOW+line+RESET for line in textwrap.wrap(safe(notice),width-2))
    available=max(1,height-len(rows)-4)
    end=max(0,len(lines)-scroll); window=lines[max(0,end-available):end]
    rows+=window+['']*(available-len(window))
    rows.append(DIM+'-'*width+RESET)
    rows.append(BLUE+' [1 Tasks] [2 Approvals] [3 Mic] [4 Help] [0 Exit]'+RESET)
    rows.append(GREEN+' YOU > '+RESET+safe(draft)[-max(1,width-9):])
    return '\033[H'+'\n'.join('\033[2K'+line for line in rows)+'\033[J'


def run(session):
    import msvcrt
    import athena_console as client
    snapshot={}; notices=deque(maxlen=12); stop=threading.Event(); lock=threading.Lock()
    draft=''; busy=False; scroll=0
    def poll():
        nonlocal snapshot
        while not stop.is_set():
            try:
                value=client.request('/v1/dashboard')
                with lock: snapshot=value
            except Exception as error:
                with lock: snapshot={'core':'offline','conversation':snapshot.get('conversation',[])}
            stop.wait(.6)
    threading.Thread(target=poll,daemon=True).start()
    def execute(text,primary=True):
        nonlocal busy,session
        try:
            if text=='/new': session='console-'+uuid4().hex; notices.append('New conversation.');return
            if text=='/help':
                notices.append('/task OBJECTIVE | /status ID | /report ID | /stop ID | /approve ID | /deny ID | /voice on|off | /new | /quit');return
            if text=='/tasks':
                for row in client.request('/v1/tasks')['tasks'][:8]:notices.append(row['id']+' '+row['state']+' '+row['objective'][:70])
                return
            if text=='/approvals':
                approvals=client.request('/v1/approvals')['approvals']
                if not approvals:notices.append('No approvals pending.')
                for row in approvals:notices.append(row['approval_id']+': '+(row.get('objective') or row.get('reason','Sensitive action'))+' | /approve '+row['approval_id'])
                return
            if text.startswith('/task '):
                row=client.request('/v1/tasks',{'objective':text[6:],'cwd':str(client.Path.cwd()),'session':session})
                notices.append('Task '+row['id']+': '+row['state']);return
            if text.startswith('/'):
                bits=text[1:].split(maxsplit=2);cmd=bits[0]
                if cmd=='voice' and len(bits)==2 and bits[1] in {'on','off'}:
                    result=client.request('/v1/voice',{'enabled':bits[1]=='on'});notices.append('Voice '+('on' if result['voice'] else 'off'));return
                if cmd in {'approve','deny','stop','resume','status','report','steer'} and len(bits)>1:
                    if cmd in {'status','report'}:result=client.request('/v1/tasks/'+bits[1])
                    else:
                        body={'action':cmd}
                        if cmd=='steer':body['text']=bits[2]
                        result=client.request('/v1/tasks/'+bits[1],body)
                    notices.append(result.get('report') or 'Task '+bits[1]+': '+result.get('state', 'approved' if result.get('accepted') else 'denied'));return
                if cmd=='doctor':notices.append(json.dumps(client.request('/v1/doctor')));return
                raise ValueError('Use /help for dashboard commands.')
            with client.request('/v1/chat',{'text':text,'session':session,'mode':'text',
                    'cwd':str(client.Path.cwd()),'request_id':uuid4().hex},stream=True) as response:
                for line in response:
                    event=json.loads(line)
                    if event['type']=='error':notices.append(event.get('error','Conversation failed.'))
        except Exception as error:notices.append(str(error))
        finally:
            if primary:busy=False
    sys.stdout.write('\033[?1049h\033[2J');sys.stdout.flush()
    previous=''
    try:
        while not stop.is_set():
            if msvcrt.kbhit():
                key=msvcrt.getwch()
                if key in {'\x00','\xe0'}:
                    special=msvcrt.getwch()
                    if special=='I':scroll+=5
                    elif special=='Q':scroll=max(0,scroll-5)
                    continue
                if key=='\x03':break
                if key=='\r':
                    text=draft.strip();draft='';scroll=0
                    text={'1':'/tasks','2':'/approvals','3':'/voice-toggle','4':'/help','0':'/quit'}.get(text,text)
                    if text.startswith('athena '):text='/'+text[7:]
                    if text in {'/quit','/exit'}:break
                    if text=='/voice-toggle':text='/voice '+('on' if snapshot.get('voice',{}).get('state','off')=='off' else 'off')
                    if not text:continue
                    if not text.startswith('/') and not snapshot.get('gateway'):
                        draft=text;notices.append('Gateway is starting or offline; your draft is retained.');continue
                    if busy and not text.startswith('/'):
                        draft=text;notices.append('A reply is in progress; your draft is retained.');continue
                    primary=not busy
                    if primary:busy=True
                    threading.Thread(target=execute,args=(text,primary),daemon=True).start()
                elif key=='\b':draft=draft[:-1]
                elif key.isprintable():draft+=key
            size=shutil.get_terminal_size((90,32))
            with lock:frame=render(snapshot,draft,list(notices),size.columns,size.lines,scroll,busy)
            if frame!=previous:sys.stdout.write(frame);sys.stdout.flush();previous=frame
            time.sleep(.04)
    finally:
        stop.set();sys.stdout.write(RESET+'\033[?1049l');sys.stdout.flush()
    print('Dashboard closed. Agent tasks continue.')
