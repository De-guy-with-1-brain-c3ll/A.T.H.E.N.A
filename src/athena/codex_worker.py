"""Codex app-server worker with native sandbox and approval events."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4


class CodexWorker:
    def __init__(self, event, approvals):
        self.event, self.approvals = event, approvals
        self.process = self.reader = None
        self.pending = {}
        self.notifications = asyncio.Queue()
        self.counter = 0
        self.thread = self.turn = None
        self.approval_tasks = set()

    async def start(self):
        executable = shutil.which('codex')
        if not executable:
            raise RuntimeError('Codex is unavailable. Select --worker openclaw explicitly.')
        env = {name: value for name, value in os.environ.items()
               if not any(marker in name.upper() for marker in ('API_KEY', 'TRANSFER_KEY', 'GATEWAY_TOKEN', 'PASSWORD'))}
        env.setdefault('HOME', str(Path.home()))
        env.setdefault('CODEX_HOME', str(Path.home() / '.codex'))
        self.process = await asyncio.create_subprocess_exec(executable, 'app-server', '--listen', 'stdio://',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.reader = asyncio.create_task(self.read())
        await self.rpc('initialize', {'clientInfo': {'name': 'athena', 'title': 'ATHENA', 'version': '0.1.0'},
                                      'capabilities': {'experimentalApi': True}})
        await self.send({'method': 'initialized'})
        account = await self.rpc('account/read', {'refreshToken': False})
        if not account.get('account'):
            raise RuntimeError('Codex needs sign-in. Run codex login; no worker was substituted.')

    async def send(self, body):
        self.process.stdin.write((json.dumps(body) + '\n').encode())
        await self.process.stdin.drain()

    async def rpc(self, method, params):
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({'id': ident, 'method': method, 'params': params})
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(ident, None)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                body = json.loads(line)
                if 'id' in body and ('result' in body or 'error' in body):
                    future = self.pending.get(body['id'])
                    if future and not future.done():
                        if 'error' in body:
                            future.set_exception(RuntimeError(body['error'].get('message', 'Codex request failed')))
                        else:
                            future.set_result(body['result'])
                elif 'id' in body:
                    task = asyncio.create_task(self.approve(body))
                    self.approval_tasks.add(task)
                    task.add_done_callback(self.approval_tasks.discard)
                else:
                    await self.notifications.put(body)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('Codex worker exited; completion is unconfirmed.'))
            await self.notifications.put({'method': 'athena/exited'})

    async def approve(self, request):
        ident = uuid4().hex[:12]
        future = asyncio.get_running_loop().create_future()
        self.approvals[ident] = future
        self.event('approval', {'approval_id': ident, 'method': request['method'],
                               'details': request.get('params', {})})
        try:
            approved = await asyncio.wait_for(future, 600)
        except TimeoutError:
            approved = False
        finally:
            self.approvals.pop(ident, None)
        if self.process.returncode is not None:
            return
        # Unknown interactive request types fail closed, never receive a blanket grant.
        method = request['method']
        if method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}:
            decisions = request.get('params', {}).get('availableDecisions', [])
            denial = 'cancel' if 'cancel' in decisions else 'decline'
            result = {'decision': 'accept' if approved else denial}
        else:
            await self.send({'id': request['id'], 'error': {'code': -32601,
                             'message': 'ATHENA does not support this approval request type.'}})
            return
        await self.send({'id': request['id'], 'result': result})

    async def run(self, objective, cwd, thread=None):
        await self.start()
        options = {'cwd': cwd, 'approvalPolicy': 'on-request',
                   'sandbox': 'workspace-write', 'persistExtendedHistory': True}
        if thread:
            result = await self.rpc('thread/resume', dict(options, threadId=thread))
        else:
            result = await self.rpc('thread/start', options)
        self.thread = result['thread']['id']
        self.event('worker_session', {'thread_id': self.thread})
        result = await self.rpc('turn/start', {'threadId': self.thread, 'input': [{'type': 'text', 'text': objective}]})
        self.turn = result['turn']['id']
        self.event('accepted', {'run_id': self.turn})
        parts = []
        async with asyncio.timeout(1800):
            while True:
                item = await self.notifications.get()
                method, params = item.get('method'), item.get('params', {})
                if method == 'athena/exited':
                    raise RuntimeError('Codex exited before a final result was received.')
                if params.get('threadId') and params['threadId'] != self.thread:
                    continue
                if method == 'item/agentMessage/delta':
                    delta = params.get('delta', '')
                    parts.append(delta)
                    self.event('text', {'text': delta})
                elif method in {'item/started', 'item/completed'}:
                    self.event('activity', {'method': method, 'item': params.get('item', {})})
                elif method == 'turn/completed':
                    turn = params['turn']
                    return turn['status'], ''.join(parts), turn.get('error')

    async def stop(self):
        if self.thread and self.turn:
            return await self.rpc('turn/interrupt', {'threadId': self.thread, 'turnId': self.turn})
        raise RuntimeError('Codex has not acknowledged an active turn yet.')

    async def steer(self, text):
        return await self.rpc('turn/steer', {'threadId': self.thread, 'expectedTurnId': self.turn,
                                           'input': [{'type': 'text', 'text': text}]})

    async def close(self):
        for task in tuple(self.approval_tasks): task.cancel()
        await asyncio.gather(*tuple(self.approval_tasks), return_exceptions=True)
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
