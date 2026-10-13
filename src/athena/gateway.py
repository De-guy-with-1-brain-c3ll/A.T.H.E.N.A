"""Persistent, authenticated OpenClaw transport; never replay a submitted turn."""
from __future__ import annotations
import asyncio
import base64
from contextlib import suppress
import hashlib
import json
from pathlib import Path
import time
import sqlite3
from uuid import uuid4
import aiohttp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from athena.openclaw import installation


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def text_content(message):
    if isinstance(message, str):
        return message
    return ''.join(item.get('text', '') for item in (message or {}).get('content', [])
                   if isinstance(item, dict) and item.get('type') == 'text')


class TextCursor:
    """Gateway chat deltas are cumulative; final events may repeat the last delta."""
    def __init__(self):
        self.text = ''

    def feed(self, text):
        if not text.startswith(self.text):
            # Revised prefixes cannot be safely replayed to a speaker.
            return ''
        delta = text[len(self.text):]
        self.text = text
        return delta


class GatewayError(RuntimeError):
    pass


class Gateway:
    def __init__(self, root=None):
        self.root = Path(root or installation())
        self.http = self.ws = self.reader = None
        self.pending = {}
        self.listeners = set()
        self.lock = asyncio.Lock()
        self.hello = None

    def identity(self):
        path = self.root / 'athena-device.json'
        if path.exists():
            raw = base64.urlsafe_b64decode(json.loads(path.read_text())['private'])
            key = Ed25519PrivateKey.from_private_bytes(raw)
        else:
            key = Ed25519PrivateKey.generate()
            raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption())
            path.write_text(json.dumps({'private': base64.urlsafe_b64encode(raw).decode()}))
            path.chmod(0o600)
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return key, public

    def auth_token(self, value):
        if isinstance(value, str):
            return value
        if value.get('source') == 'env':
            from athena.openclaw import environment
            return environment()[value['id']]
        if value.get('source') == 'store':
            db = sqlite3.connect((self.root / 'state/state/openclaw.sqlite').resolve().as_uri() + '?mode=ro', uri=True)
            try:
                row = db.execute("SELECT value FROM secret_store_entries WHERE name=? AND scope_kind='team' "
                    "AND scope_id='' AND deleted_at_ms IS NULL", (value['id'],)).fetchone()
                if row:
                    return row[0]
            finally:
                db.close()
        raise GatewayError('Gateway token reference could not be resolved locally.')

    async def connect(self):
        async with self.lock:
            if self.ws is not None and not self.ws.closed and self.hello:
                return self.hello
            await self._close_transport()
            config = json.loads((self.root / 'state/openclaw.json').read_text(encoding='utf-8'))
            token = self.auth_token(config['gateway']['auth']['token'])
            port = config['gateway'].get('port', 18789)
            self.http = aiohttp.ClientSession()
            try:
                self.ws = await self.http.ws_connect(f'http://127.0.0.1:{port}', heartbeat=20,
                    max_msg_size=8 * 1024 * 1024, timeout=aiohttp.ClientWSTimeout(ws_receive=90))
                challenge = await asyncio.wait_for(self.ws.receive_json(), 10)
                if challenge.get('event') != 'connect.challenge':
                    raise GatewayError('OpenClaw did not send its authentication challenge.')
                key, public = self.identity()
                scopes = ['operator.read', 'operator.write', 'operator.admin', 'operator.approvals']
                stamp = challenge['payload']['ts']
                nonce = challenge['payload']['nonce']
                device_id = hashlib.sha256(public).hexdigest()
                signed = '|'.join(['v3', device_id, 'cli', 'cli', 'operator', ','.join(scopes),
                                   str(stamp), token, nonce, 'win32', ''])
                ident = uuid4().hex
                await self.ws.send_json({'type': 'req', 'id': ident, 'method': 'connect', 'params': {
                    'minProtocol': 4, 'maxProtocol': 4,
                    'client': {'id': 'cli', 'version': 'athena-1', 'platform': 'win32', 'mode': 'cli'},
                    'role': 'operator', 'scopes': scopes, 'caps': ['tool-events'],
                    'auth': {'token': token}, 'device': {'id': device_id, 'publicKey': b64(public),
                        'signature': b64(key.sign(signed.encode())), 'signedAt': stamp, 'nonce': nonce}}})
                while True:
                    frame = await asyncio.wait_for(self.ws.receive_json(), 15)
                    if frame.get('id') == ident:
                        if not frame.get('ok'):
                            raise GatewayError(frame.get('error', {}).get('message', 'Gateway authentication failed'))
                        self.hello = frame['payload']
                        break
                self.reader = asyncio.create_task(self._read())
                return self.hello
            except BaseException:
                await self._close_transport()
                raise

    async def _read(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                frame = json.loads(message.data)
                if frame.get('type') == 'res':
                    future = self.pending.get(frame.get('id'))
                    if future and not future.done():
                        if frame.get('ok'):
                            future.set_result(frame.get('payload'))
                        else:
                            future.set_exception(GatewayError(frame.get('error', {}).get('message', 'RPC failed')))
                elif frame.get('type') == 'event':
                    for queue in tuple(self.listeners):
                        if queue.full():
                            self.listeners.discard(queue)
                            queue.get_nowait()
                            queue.put_nowait({'event': 'athena.disconnected', 'payload': {'reason': 'slow_consumer'}})
                        else:
                            queue.put_nowait(frame)
        except (aiohttp.ClientError, ValueError):
            pass
        finally:
            self.hello = None
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(GatewayError('Gateway disconnected; submission state is uncertain.'))
            for queue in tuple(self.listeners):
                if not queue.full():
                    queue.put_nowait({'event': 'athena.disconnected', 'payload': {}})

    async def rpc(self, method, params=None, timeout=20):
        await self.connect()
        ident = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.ws.send_json({'type': 'req', 'id': ident, 'method': method, 'params': params or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(ident, None)

    async def stream(self, session, message, request_id=None, timeout=1800, queue_mode='followup'):
        queue = asyncio.Queue(maxsize=1024)
        self.listeners.add(queue)
        cursor = TextCursor()
        seen = set()
        run_id = request_id or uuid4().hex
        started = time.perf_counter()
        try:
            receipt = await self.rpc('chat.send', {'sessionKey': session, 'message': message,
                'idempotencyKey': run_id, 'thinking': 'off', 'queueMode': queue_mode, 'timeoutMs': timeout * 1000})
            actual = receipt.get('runId', run_id)
            yield {'type': 'accepted', 'run_id': actual, 'elapsed': time.perf_counter() - started}
            async with asyncio.timeout(timeout + 30):
                while True:
                    frame = await queue.get()
                    if frame['event'] == 'athena.disconnected':
                        raise GatewayError('Connection lost. Check this run before continuing; it was not resubmitted.')
                    payload = frame.get('payload') or {}
                    if payload.get('runId') != actual:
                        continue
                    # OpenClaw's final chat event shares the last delta's seq.
                    # Dedupe within each event phase, never across delta/final.
                    sequence = (frame['event'], payload.get('seq'), payload.get('state'), payload.get('stream'))
                    if sequence[1] is not None:
                        if sequence in seen: continue
                        seen.add(sequence)
                    if frame['event'] == 'agent':
                        if payload.get('stream') == 'tool':
                            yield {'type': 'activity', 'data': payload.get('data', {})}
                    elif frame['event'] == 'chat':
                        text = text_content(payload.get('message'))
                        if payload.get('replace') and not text.startswith(cursor.text):
                            cursor.text = text
                            yield {'type': 'replace', 'text': text}
                            delta = ''
                        else:
                            delta = cursor.feed(text)
                        if delta:
                            yield {'type': 'text', 'text': delta, 'elapsed': time.perf_counter() - started}
                        state = payload.get('state')
                        if state in {'final', 'error', 'aborted'}:
                            yield {'type': state, 'text': cursor.text,
                                   'error': payload.get('errorMessage'), 'elapsed': time.perf_counter() - started}
                            return
        finally:
            self.listeners.discard(queue)

    async def _close_transport(self):
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
        if self.ws:
            await self.ws.close()
        if self.http:
            await self.http.close()
        self.reader = self.ws = self.http = self.hello = None

    async def close(self):
        await self._close_transport()
