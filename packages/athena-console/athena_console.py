"""Standard-library-only console; no speech or model dependencies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4


def connection():
    path = Path(os.environ.get('ATHENA_CONNECTION_FILE') or
                Path(os.environ.get('USERPROFILE', Path.home())) / '.athena/connection.json')
    try:
        pointer = json.loads(path.read_text(encoding='utf-8-sig'))
        state = json.loads(Path(pointer['control']).read_text(encoding='utf-8'))
        return pointer, state
    except (OSError, ValueError):
        raise RuntimeError('ATHENA is not installed or started. Run the Windows installer, then athena start.') from None


def request(path, body=None, stream=False):
    _, state = connection()
    req = urllib.request.Request(state['url'] + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': 'Bearer ' + state['token'], 'Content-Type': 'application/json'})
    try:
        response = urllib.request.urlopen(req, timeout=240 if stream else 30)
    except urllib.error.HTTPError as error:
        raw = error.read().decode(errors='replace')
        try:
            raise RuntimeError(json.loads(raw)['error']) from None
        except (ValueError, KeyError):
            raise RuntimeError(f'ATHENA returned HTTP {error.code}.') from None
    except urllib.error.URLError:
        raise RuntimeError('ATHENA core is offline. Run athena start.') from None
    if stream:
        return response
    with response:
        return json.load(response)


def display(value, machine=False):
    if machine:
        print(json.dumps(value))
    elif isinstance(value, dict) and 'tasks' in value:
        for row in value['tasks']:
            print(f"{row['id']}  {row['state']:20} {row['worker']:9} {row['objective'][:90]}")
    elif isinstance(value, dict) and 'objective' in value:
        print(f"Task {value['id']}: {value['state']} ({value['worker']})")
        print('Objective:',value['objective'])
        print('Workspace:', value['cwd'])
        if value.get('report'):
            print(value['report'])
        if value.get('verification'):
            print('Independent checks:', json.dumps(value['verification']))
        if value['state'] == 'awaiting_approval':
            print(f"Approve this scope with: athena approve {value['id']}")
    elif isinstance(value,dict) and 'accepted' in value and 'approval_id' in value:
        print('Approved. The worker can continue.' if value['accepted'] else 'Denied.')
    elif isinstance(value,dict) and 'voice' in value and isinstance(value['voice'],bool):
        print('Voice: '+('on' if value['voice'] else 'off')+(' ('+value['state']+')' if value.get('state') else ''))
    else:
        print(json.dumps(value, indent=2))


def default_session():
    workspace = str(Path.cwd().resolve()).casefold().encode()
    return 'console-v3-' + hashlib.sha256(workspace).hexdigest()[:16]


def ask(text, session, machine):
    received = False
    with request('/v1/chat', {'text': text, 'session': session, 'mode':'text',
            'cwd':str(Path.cwd()), 'request_id': uuid4().hex}, stream=True) as response:
        for line in response:
            event = json.loads(line)
            if machine:
                print(json.dumps(event), flush=True)
            elif event['type'] == 'text':
                received = True
                print(event['text'], end='', flush=True)
            elif event['type'] == 'replace':
                print('\n[Updated response] ' + event['text'], end='', flush=True)
            elif event['type'] == 'error':
                raise RuntimeError(event.get('error') or 'Conversation failed.')
    if not machine:
        if not received: print('[No assistant response received. Try athena doctor.]', end='')
        print()


BANNER = r'''
      _   _____ _   _ _____ _   _    _
     / \ |_   _| | | | ____| \ | |  / \
    / _ \  | | | |_| |  _| |  \| | / _ \
   / ___ \ | | |  _  | |___| |\  |/ ___ \
  /_/   \_\|_| |_| |_|_____|_| \_/_/   \_\

  +--------------------------------------------------------+
  | [1] Tasks  [2] Approvals  [3] Voice  [4] Help  [0] Exit |
  +--------------------------------------------------------+
'''


def interactive(session, machine=False):
    print(BANNER)
    print('Workspace:', Path.cwd())
    print('Type normally to chat. Buttons use their number; commands start with /.')
    print('Approvals: /approve ID  |  New conversation: /new\n')
    buttons = {'1':'/tasks','2':'/approvals','3':'/voice','4':'/help','0':'/quit'}
    while True:
        try: text = input('YOU > ').strip()
        except EOFError: return
        if not text: continue
        text = buttons.get(text, text)
        if text.startswith('athena '): text = '/' + text[7:]
        if text in {'/quit','/exit'}: return
        try:
            if text == '/new':
                session = 'console-' + uuid4().hex
                print('New conversation. Existing tasks are retained.'); continue
            if text == '/help':
                print('/tasks  /task OBJECTIVE  /status ID  /report ID  /approve ID  /deny ID\n'
                      '/steer ID TEXT  /stop ID  /resume ID  /voice on|off  /doctor  /new  /quit'); continue
            if text in {'/tasks','/approvals'}:
                rows = request('/v1/tasks')['tasks']
                if text == '/approvals':
                    pending = request('/v1/approvals')['approvals']
                    if not pending: print('No approvals pending.')
                    for approval in pending:
                        print(approval['approval_id'], approval.get('objective') or approval.get('reason','Action approval'))
                        if approval.get('cwd'): print('  Workspace:',approval['cwd'])
                        if approval.get('path'): print('  File:',approval['path'])
                        details = approval.get('details',{})
                        if details.get('command'): print('  Command:',details['command'])
                        print('  /approve ' + approval['approval_id'] + '  /deny ' + approval['approval_id'])
                else:
                    display({'tasks':rows},machine)
                    if not rows: print('No tasks yet.')
                continue
            if text == '/voice':
                health = request('/v1/health')
                print('Voice:', 'on' if health['voice'] else 'off', '| /voice on | /voice off'); continue
            if text.startswith('/task '):
                display(request('/v1/tasks', {'objective':text[6:], 'cwd':str(Path.cwd()), 'session':session}),machine); continue
            if text.startswith('/'):
                pieces = text[1:].split(maxsplit=2); command = pieces[0]
                if command in {'approve','deny','stop','resume','status','report','steer'}:
                    if len(pieces)<2: raise ValueError('Use /' + command + ' ID' + (' TEXT' if command=='steer' else ''))
                    if command in {'status','report'}: display(request('/v1/tasks/'+pieces[1]),machine)
                    else:
                        body={'action':command}
                        if command=='steer':
                            if len(pieces)<3: raise ValueError('Use /steer ID TEXT')
                            body['text']=pieces[2]
                        display(request('/v1/tasks/'+pieces[1],body),machine)
                    continue
                if command=='voice' and len(pieces)==2 and pieces[1] in {'on','off'}:
                    display(request('/v1/voice',{'enabled':pieces[1]=='on'}),machine); continue
                if command=='doctor': display(request('/v1/doctor'),machine); continue
                raise ValueError('Unknown console command. Use /help.')
            print('ATHENA > ',end='',flush=True)
            ask(text,session,machine)
        except (RuntimeError,OSError,ValueError,KeyError) as error:
            print('ATHENA:',str(error))


def start():
    try:
        display(request('/v1/health'))
        return
    except RuntimeError:
        pass
    path = Path(os.environ.get('ATHENA_CONNECTION_FILE') or
        Path(os.environ.get('USERPROFILE', Path.home())) / '.athena/connection.json')
    pointer = json.loads(path.read_text(encoding='utf-8-sig'))
    root = Path(pointer['source'])
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    import socket
    with socket.socket() as sock:
        active = sock.connect_ex(('127.0.0.1', 18789)) == 0
    if not active:
        with open(root / 'data/openclaw/console-gateway.log', 'ab') as log:
            subprocess.Popen([pointer['python'], '-m', 'athena.openclaw', 'gateway', 'run'],
                cwd=root, stdout=log, stderr=log, creationflags=flags)
    with open(root / 'data/openclaw/core.log', 'ab') as log:
        subprocess.Popen([pointer['python'], '-m', 'athena.core'], cwd=root,
            stdout=log, stderr=log, creationflags=flags)
    for _ in range(30):
        try:
            health = request('/v1/health')
            display(health)
            return
        except RuntimeError:
            time.sleep(1)
    raise RuntimeError('Core did not become ready. Check data/openclaw/core.log.')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith('/'): argv[0] = argv[0][1:]
    if sys.stdout.isatty() and hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    parser = argparse.ArgumentParser(description='ATHENA console — shared voice and agent tasks')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--session', default=None)
    sub = parser.add_subparsers(dest='command')
    chat = sub.add_parser('ask'); chat.add_argument('text',nargs='+'); chat.add_argument('--session',default=argparse.SUPPRESS); chat.add_argument('--json', action='store_true')
    task = sub.add_parser('task'); task.add_argument('objective'); task.add_argument('--worker', choices=['codex', 'openclaw'])
    task.add_argument('--cwd', default=str(Path.cwd())); task.add_argument('--checks', type=Path); task.add_argument('--json', action='store_true')
    sub.add_parser('tasks')
    improve = sub.add_parser('improve'); improve.add_argument('objective'); improve.add_argument('--checks', type=Path)
    for name in ('status', 'report', 'attach', 'stop', 'resume', 'reconcile', 'verify', 'approve', 'deny', 'steer', 'install', 'rollback'):
        cmd = sub.add_parser(name); cmd.add_argument('id'); cmd.add_argument('--json', action='store_true')
        if name == 'steer': cmd.add_argument('text')
        if name == 'resume': cmd.add_argument('--text', default='Continue the approved task.')
    for name in ('start', 'restart', 'doctor'): sub.add_parser(name)
    # Stop without an ID stops the service; stop <id> stops a task.
    for action in sub.choices.values():
        if action.prog.endswith(' stop'):
            action._actions[1].nargs = '?'
    voice = sub.add_parser('voice'); voice.add_argument('mode', choices=['on', 'off'])
    routine = sub.add_parser('routines'); routine.add_argument('action', nargs='?', choices=['list', 'add', 'update', 'remove', 'run'], default='list')
    routine.add_argument('--file', type=Path); routine.add_argument('--id')
    for command_parser in sub.choices.values():
        if not any('--json' in action.option_strings for action in command_parser._actions):
            command_parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        for action in command_parser._actions:
            if '--json' in action.option_strings:
                action.default = argparse.SUPPRESS
    args = parser.parse_args(argv)
    machine = args.json
    try:
        if args.command is None:
            from athena_dashboard import enable_terminal,run
            if not machine and enable_terminal():run(args.session or default_session())
            else:interactive(args.session or default_session(),machine)
        elif args.command == 'ask':
            ask(' '.join(args.text), args.session or default_session(), machine)
        elif args.command == 'task':
            body = {'objective': args.objective, 'worker': args.worker, 'cwd': args.cwd, 'request_id': uuid4().hex,
                    'session': args.session or default_session()}
            if args.checks: body['checks'] = json.loads(args.checks.read_text())
            display(request('/v1/tasks', body), machine)
        elif args.command == 'improve':
            pointer, _ = connection()
            body = {'objective': args.objective, 'cwd': pointer['source'], 'worker': 'codex', 'self_update': True}
            if args.checks: body['checks'] = json.loads(args.checks.read_text())
            display(request('/v1/tasks', body), machine)
        elif args.command == 'tasks': display(request('/v1/tasks'), machine)
        elif args.command in {'status', 'report'}: display(request('/v1/tasks/' + args.id), machine)
        elif args.command == 'attach':
            cursor = 0
            while True:
                events = request('/v1/events?' + urllib.parse.urlencode({'task': args.id, 'after': cursor}))['events']
                for event in events:
                    cursor = event['seq']; display(event, machine)
                if request('/v1/tasks/' + args.id)['state'] in {'verified', 'reply_received', 'failed', 'cancelled', 'denied', 'unconfirmed'}: break
                time.sleep(1)
        elif args.command == 'stop' and args.id is None: display(request('/v1/service', {'action': 'stop'}), machine)
        elif args.command in {'stop', 'resume', 'reconcile', 'verify', 'approve', 'deny', 'steer', 'install', 'rollback'}:
            body = {'action': args.command}
            if hasattr(args, 'text'): body['text'] = args.text
            display(request('/v1/tasks/' + args.id, body), machine)
        elif args.command == 'start': start()
        elif args.command == 'restart':
            request('/v1/service', {'action': 'stop'}); time.sleep(2); start()
        elif args.command == 'doctor': display(request('/v1/doctor'), machine)
        elif args.command == 'voice': display(request('/v1/voice', {'enabled': args.mode == 'on'}), machine)
        elif args.command == 'routines':
            if args.action == 'list': display(request('/v1/routines'), machine)
            else:
                body = json.loads(args.file.read_text()) if args.file else {'id': args.id}
                body['action'] = args.action
                display(request('/v1/routines', body), machine)
        return 0
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'error': str(error)}) if machine else 'ATHENA: ' + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nDetached. Agent tasks continue in the core.')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
