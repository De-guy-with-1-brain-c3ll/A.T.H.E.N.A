"""Check the paired Pi's real OpenClaw task flow with a harmless coding artifact."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4

from athena.desktop import Client, read_preferences


def chat(client, text):
    started = client.request('/api/chat', {'text': text})
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = client.request('/api/chat/' + started['id'])
        if result['status'] == 'complete':
            return result['answer']
        time.sleep(1)
    raise RuntimeError('Pi chat did not finish within 90 seconds; no request was retried.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coding', action='store_true', help='Submit one authorized synthetic coding task')
    parser.add_argument('--task', default='6c5530b5f293')
    args = parser.parse_args()
    preferences = read_preferences()
    client = Client(preferences['host'], preferences['fingerprint'])
    bootstrap = client.login('')
    health = client.request('/health')
    print('Pi health:', json.dumps(health), flush=True)
    assert {'openclaw_task', 'openclaw_status'} <= set(bootstrap['tools']), 'Pi OpenClaw tools missing'
    print('Pi task and status tools are registered.', flush=True)
    answer = chat(client, f'What is the status of OpenClaw task {args.task}?')
    print('Pi status:', answer, flush=True)
    assert args.task in answer and 'reply_received' in answer, 'Known completed task status was not retrieved'
    if not args.coding:
        return
    root = Path(__file__).resolve().parents[1]
    artifact = root / 'data/openclaw/workspace' / ('pi-coding-check-' + uuid4().hex[:8] + '.py')
    objective = (f'Use OpenClaw to write a standalone Python program at {artifact.as_posix()} '
        'that defines add(a,b), asserts add(2,3)==5 and prints exactly PI_OPENCLAW_CODING_OK '
        'when run. Run it using the provided Python executable and report the actual file path, '
        'output and exit code. Only create this test file; do not change ATHENA source or other files.')
    prepared = chat(client, objective)
    print('Pi prepared:', prepared, flush=True)
    # This synthetic verification task is explicitly within the authorized repair.
    queued = chat(client, 'yes')
    print('Pi submitted:', queued, flush=True)
    match = re.search(r'OpenClaw task ([a-f0-9]{12}) queued', queued)
    if not match:
        raise RuntimeError('Pi did not confirm submission; no task was retried.')
    ident = match.group(1)
    deadline = time.monotonic() + 340
    previous = None
    while time.monotonic() < deadline:
        answer = chat(client, f'What is the status of OpenClaw task {ident}?')
        if answer != previous:
            print('Pi progress:', answer, flush=True)
            previous = answer
        if 'reply_received' in answer:
            break
        if 'unconfirmed' in answer or 'could not be verified' in answer:
            raise RuntimeError(answer)
        time.sleep(3)
    else:
        raise RuntimeError('Final reply was not received within the check deadline; no resubmission.')
    report = chat(client, f'Give me the final report on OpenClaw task {ident}.')
    print('Pi final report:', report, flush=True)
    assert ident in report and 'PI_OPENCLAW_CODING_OK' in report
    checked = subprocess.run([sys.executable, str(artifact)], capture_output=True,
        text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    assert checked.returncode == 0 and checked.stdout.strip() == 'PI_OPENCLAW_CODING_OK', checked.stderr
    print('Independently verified the created file, output and exit code.', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
