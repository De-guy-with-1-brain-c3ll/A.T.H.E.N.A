"""Native ATHENA controller. Standard-library GUI; pinned HTTPS, no API keys."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import queue
import socket
import ssl
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import webbrowser


def private_host(host):
    address = ipaddress.ip_address(host)
    return address.version == 4 and any(address in ipaddress.ip_network(net)
        for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, fingerprint=None, timeout=6):
        if not private_host(host):
            raise ValueError('Use the Pi’s private LAN IPv4 address, without https:// or a port.')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(host, 8780, timeout=timeout, context=context)
        self.fingerprint = fingerprint

    def connect(self):
        super().connect()
        self.observed = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if self.fingerprint and not hmac.compare_digest(self.observed, self.fingerprint):
            self.close()
            raise ssl.SSLError('Pi certificate changed. No password or command was sent. Pair again only after verifying the Pi.')


def probe(host, timeout=.6):
    connection = PinnedConnection(host, timeout=timeout)
    try:
        connection.request('GET', '/health')
        response = connection.getresponse()
        payload = json.loads(response.read(4096))
        if response.status != 200 or payload.get('app') != 'athena':
            raise ValueError('This is not an updated ATHENA controller.')
        return host, connection.observed
    finally:
        connection.close()


def discover(cidr):
    network = ipaddress.ip_network(cidr, strict=False)
    if network.version != 4 or network.prefixlen < 24 or not private_host(str(network.network_address)):
        raise ValueError('Enter a private LAN subnet no larger than /24, for example 192.168.33.0/24.')
    matches = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = [pool.submit(probe, str(host)) for host in network.hosts()]
        for future in as_completed(futures):
            try:
                matches.append(future.result())
            except (OSError, ValueError, http.client.HTTPException):
                pass
    return matches


class Client:
    def __init__(self, host, fingerprint):
        self.host, self.fingerprint = host, fingerprint
        self.cookie = ''; self.csrf = ''

    def request(self, path, body=None):
        connection = PinnedConnection(self.host, self.fingerprint)
        try:
            headers = {'Accept': 'application/json'}
            if self.cookie: headers['Cookie'] = self.cookie
            if self.csrf: headers['X-ATHENA-CSRF'] = self.csrf
            if body is not None: headers['Content-Type'] = 'application/json'
            connection.request('GET' if body is None else 'POST', path,
                json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024: raise ValueError('Pi response exceeded safe size.')
            if response.status >= 400:
                raise ValueError(f'HTTP {response.status}: {raw.decode(errors="replace")[:300]}')
            cookie = response.getheader('Set-Cookie')
            if cookie: self.cookie = cookie.split(';', 1)[0]
            if path == '/': return {}
            result = json.loads(raw)
            if 'csrf' in result: self.csrf = result['csrf']
            return result
        finally:
            connection.close()

    def login(self, password):
        # Even password-disabled dashboards require a signed session for writes.
        if password: self.request('/api/login', {'password': password})
        else: self.request('/')
        return self.request('/api/bootstrap')


def preference_path():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ATHENA Control' / 'connection.json'


def read_preferences():
    try: return json.loads(preference_path().read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}


def save_preferences(host, fingerprint):
    path = preference_path(); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'host': host, 'fingerprint': fingerprint}), encoding='utf-8')
    temporary.replace(path)  # Never persist the dashboard password or session.


def local_subnet():
    connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        connection.connect(('192.168.33.1', 9))
        host = connection.getsockname()[0]
        if private_host(host): return str(ipaddress.ip_network(host + '/24', strict=False))
    except OSError: pass
    finally: connection.close()
    return '192.168.33.0/24'


class App:
    def __init__(self, root):
        self.root = root; self.client = None; self.polling = False; self.closed = False
        self.generation = 0; self.events = queue.Queue(); self.pool = ThreadPoolExecutor(max_workers=4)
        self.chat_jobs = {}; self.settings = {}; self.setting_vars = {}
        root.title('ATHENA • Control'); root.geometry('1150x790'); root.minsize(850, 620)
        root.configure(bg='#07151d')
        style = ttk.Style(root); style.theme_use('clam')
        style.configure('.', background='#0c202a', foreground='#d4eef5', font=('Segoe UI', 10))
        style.configure('TFrame', background='#0c202a')
        style.configure('TLabel', background='#0c202a', foreground='#d4eef5')
        style.configure('TButton', padding=(12, 8), background='#183f50')
        style.map('TButton', background=[('active', '#24576a')])
        style.configure('TEntry', fieldbackground='#102e3b', foreground='#d4eef5')
        style.configure('TCombobox', fieldbackground='#102e3b', foreground='#d4eef5')
        style.configure('Treeview', background='#102630', fieldbackground='#102630', foreground='#d4eef5', rowheight=30)
        style.configure('Treeview.Heading', background='#184353', foreground='#55dded')
        style.configure('TNotebook.Tab', padding=(15, 10))
        style.map('Treeview', background=[('selected', '#255c6d')])
        outer = ttk.Frame(root, padding=18); outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='A T H E N A', foreground='#54e1f3', font=('Segoe UI', 26, 'bold')).pack(anchor='w')
        self.status = tk.StringVar(value='Connect to your Pi. Monitoring is silent and uses no model tokens.')
        ttk.Label(outer, textvariable=self.status).pack(anchor='w', pady=(0, 12))
        connect = ttk.Frame(outer); connect.pack(fill='x')
        saved = read_preferences(); self.saved = saved
        self.host = tk.StringVar(value=saved.get('host', '192.168.33.153'))
        self.password = tk.StringVar(); self.subnet = tk.StringVar(value=local_subnet())
        ttk.Label(connect, text='Pi IP').pack(side='left')
        ttk.Entry(connect, textvariable=self.host, width=17).pack(side='left', padx=6)
        ttk.Label(connect, text='Dashboard password').pack(side='left')
        ttk.Entry(connect, textvariable=self.password, show='•', width=19).pack(side='left', padx=6)
        ttk.Button(connect, text='Connect', command=self.connect).pack(side='left', padx=4)
        ttk.Entry(connect, textvariable=self.subnet, width=20).pack(side='left', padx=6)
        ttk.Button(connect, text='Find Pi', command=self.scan).pack(side='left')
        book = ttk.Notebook(outer); book.pack(fill='both', expand=True, pady=14)
        self.tabs = {}
        for name in ('Overview', 'Tasks', 'Subagents', 'Settings', 'Prompts', 'Chat', 'Usage & delay'):
            pane = ttk.Frame(book, padding=12); book.add(pane, text=name); self.tabs[name] = pane
        self.overview(); self.tasks = self.table(self.tabs['Tasks'], ('Tool / file', 'State', 'Progress', 'Details'))
        self.agents = self.table(self.tabs['Subagents'], ('ID', 'State', 'Progress', 'Report'))
        self.settings_ui(); self.prompts_ui(); self.chat_ui()
        self.metrics = self.text(self.tabs['Usage & delay'])
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(80, self.drain); self.root.after(1500, self.poll)

    def text(self, pane, height=None):
        widget = tk.Text(pane, bg='#0a1c26', fg='#d8f1f6', insertbackground='white',
            wrap='word', relief='flat', padx=12, pady=10, font=('Consolas', 11), height=height or 10)
        widget.pack(fill='both', expand=True)
        return widget

    def table(self, pane, columns):
        tree = ttk.Treeview(pane, columns=columns, show='headings')
        for column in columns:
            tree.heading(column, text=column); tree.column(column, width=190, minwidth=80)
        scroll = ttk.Scrollbar(pane, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scroll.set); scroll.pack(side='right', fill='y'); tree.pack(fill='both', expand=True)
        return tree

    def overview(self):
        pane = self.tabs['Overview']; row = ttk.Frame(pane); row.pack(fill='x', pady=10)
        for label, action in (('Voice ON', 'start'), ('Voice OFF', 'stop'), ('Restart voice', 'restart'), ('Restart entire ATHENA', 'restart_all')):
            ttk.Button(row, text=label, command=lambda a=action: self.control(a)).pack(side='left', padx=4)
        ttk.Button(row, text='Reboot Pi…', command=self.reboot).pack(side='left', padx=4)
        self.voice_status = tk.StringVar(value='Voice: not connected')
        ttk.Label(pane, textvariable=self.voice_status, font=('Segoe UI', 16)).pack(anchor='w', pady=12)
        for label, target in (('Use Pi audio', 'pi'), ('Use computer audio', 'computer')):
            ttk.Button(pane, text=label, command=lambda t=target: self.api('/api/audio-route', {'target': t})).pack(anchor='w', pady=4)
        ttk.Label(pane, text='Computer microphone streaming still uses the browser audio page; keep that page open.').pack(anchor='w', pady=6)
        ttk.Button(pane, text='Open browser audio page', command=self.open_browser).pack(anchor='w')
        ttk.Button(pane, text='PC keyboard control…', command=self.keyboard).pack(anchor='w', pady=6)
        music = ttk.Frame(pane); music.pack(fill='x', pady=12)
        for label, action in (('Pause music', 'pause'), ('Resume', 'resume'), ('Next track', 'next'), ('Stop music', 'stop')):
            ttk.Button(music, text=label, command=lambda a=action: self.api('/api/music', {'action': a})).pack(side='left', padx=4)
        volume = ttk.Frame(pane); volume.pack(fill='x', pady=4)
        ttk.Label(volume, text='Speaker volume (0–100)').pack(side='left')
        self.volume = tk.StringVar(value='40')
        ttk.Spinbox(volume, from_=0, to=100, textvariable=self.volume, width=6).pack(side='left', padx=8)
        ttk.Button(volume, text='Apply volume', command=self.set_volume).pack(side='left')
        self.summary = self.text(pane)

    def settings_ui(self):
        pane = self.tabs['Settings']
        ttk.Label(pane, text='Every tunable runtime setting. Live settings apply automatically; others require restart.').pack(anchor='w')
        ttk.Button(pane, text='Load / refresh settings', command=self.load_settings).pack(anchor='w', pady=8)
        canvas = tk.Canvas(pane, bg='#0c202a', highlightthickness=0)
        scrollbar = ttk.Scrollbar(pane, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set); scrollbar.pack(side='right', fill='y'); canvas.pack(fill='both', expand=True)
        self.settings_frame = ttk.Frame(canvas); item = canvas.create_window((0, 0), window=self.settings_frame, anchor='nw')
        self.settings_frame.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(item, width=e.width))

    def prompts_ui(self):
        pane = self.tabs['Prompts']
        ttk.Label(pane, text='System prompt').pack(anchor='w'); self.system_prompt = self.text(pane)
        ttk.Label(pane, text='Memory prompt').pack(anchor='w', pady=5); self.memory_prompt = self.text(pane)
        ttk.Button(pane, text='Save prompts', command=self.save_prompts).pack(anchor='w', pady=8)

    def chat_ui(self):
        pane = self.tabs['Chat']; self.chat_log = self.text(pane)
        row = ttk.Frame(pane); row.pack(fill='x', pady=8)
        self.message = tk.StringVar(); entry = ttk.Entry(row, textvariable=self.message)
        entry.pack(side='left', fill='x', expand=True); entry.bind('<Return>', lambda e: self.send_chat())
        ttk.Button(row, text='Send', command=self.send_chat).pack(side='left', padx=5)
        ttk.Button(row, text='Recent conversations', command=lambda: self.api('/api/conversations', callback=self.show_conversations)).pack(side='left')

    def background(self, work, success=None, fail=None):
        generation = self.generation
        def run():
            try: self.events.put((generation, success, work(), None, fail))
            except Exception as error: self.events.put((generation, None, None, str(error), fail))
        self.pool.submit(run)

    def drain(self):
        if self.closed: return
        for _ in range(50):
            try: generation, callback, result, error, fail = self.events.get_nowait()
            except queue.Empty: break
            if generation != self.generation: continue
            if error:
                if fail: fail(error)
                else: self.status.set(error)
            elif callback:
                try: callback(result)
                except Exception as error: self.status.set(str(error))
        self.root.after(80, self.drain)

    def api(self, path, body=None, callback=None):
        client = self.client
        if not client: self.status.set('Connect first.'); return
        self.background(lambda: client.request(path, body), callback or (lambda result: self.status.set(result.get('message', 'Command accepted.'))))

    def connect(self):
        host, password = self.host.get().strip(), self.password.get()
        self.generation += 1; self.client = None; self.polling = False
        self.status.set('Checking the Pi certificate…')
        def verified(result):
            address, fingerprint = result
            trusted = self.saved.get('fingerprint') == fingerprint
            if not trusted and not messagebox.askyesno('Pair with this Pi?',
                    f'Pi: {address}\nSHA-256 certificate:\n{fingerprint}\n\nOnly accept if this is your Pi on a trusted network. No credentials have been sent.'):
                self.status.set('Pairing cancelled.'); return
            client = Client(address, fingerprint)
            def logged_in(bootstrap):
                self.client = client; self.saved = {'host': address, 'fingerprint': fingerprint}
                save_preferences(address, fingerprint); self.password.set('')
                self.status.set('Connected • silent local monitoring • no model calls')
                self.voice_status.set(bootstrap.get('serviceLabel', 'Connected'))
                prompts = bootstrap.get('prompts', {})
                self.replace(self.system_prompt, prompts.get('system', ''))
                self.replace(self.memory_prompt, prompts.get('memory', ''))
                self.load_settings()
            self.background(lambda: client.login(password), logged_in)
        self.background(lambda: probe(host, 4), verified)

    def scan(self):
        cidr = self.subnet.get().strip(); self.status.set('Scanning this LAN subnet…')
        def found(matches):
            if not matches: self.status.set('No updated Pi found. Check the subnet, power, and Wi-Fi.'); return
            same = [item for item in matches if item[1] == self.saved.get('fingerprint')]
            candidates = same or matches
            if len(candidates) == 1: address = candidates[0][0]
            else:
                address = simpledialog.askstring('Choose Pi', 'Found: ' + ', '.join(item[0] for item in candidates))
                if address not in {item[0] for item in candidates}: return
            self.host.set(address); self.status.set('Pi found. Click Connect to sign in.')
        self.background(lambda: discover(cidr), found)

    def control(self, action):
        if action == 'restart_all' and not messagebox.askyesno('Restart ATHENA?', 'Restart voice, Feishu, and the web controller? Running work may be interrupted.'): return
        self.api('/api/service', {'action': action}, lambda result: self.status.set(result.get('serviceLabel', 'Restart requested.')))

    def reboot(self):
        if simpledialog.askstring('Reboot Pi', 'Running work will stop. Type REBOOT PI to confirm:') == 'REBOOT PI':
            self.api('/api/reboot', {'confirmation': 'REBOOT PI'})

    def keyboard(self):
        enabled = messagebox.askyesno('PC keyboard control', 'Enable ATHENA keyboard control on your PC? Select No to disable.')
        self.api('/api/pc/keyboard', {'enabled': enabled})

    def set_volume(self):
        try:
            value = int(self.volume.get())
            if not 0 <= value <= 100: raise ValueError()
        except ValueError:
            self.status.set('Choose volume between 0 and 100.'); return
        self.api('/api/volume', {'value': value})

    def open_browser(self):
        if self.client: webbrowser.open('https://' + self.client.host + ':8780/')

    def poll(self):
        if self.closed: return
        if self.client and not self.polling:
            self.polling = True; client = self.client
            def work():
                started = time.monotonic(); monitor = client.request('/api/monitor')
                rtt = (time.monotonic() - started) * 1000
                return monitor, rtt, client.request('/api/status')
            def failed(error): self.polling = False; self.status.set('Connection unavailable: ' + error)
            self.background(work, self.show_monitor, failed)
            for identity in list(self.chat_jobs):
                self.api('/api/chat/' + identity, callback=lambda result, i=identity: self.chat_result(i, result))
        self.root.after(2000, self.poll)

    @staticmethod
    def replace(widget, value):
        widget.delete('1.0', 'end'); widget.insert('end', value)

    @staticmethod
    def set_rows(tree, rows):
        tree.delete(*tree.get_children())
        for row in rows: tree.insert('', 'end', values=row)

    def show_monitor(self, result):
        self.polling = False
        monitor, rtt, status = result; self.voice_status.set(status.get('serviceLabel', 'Unknown'))
        self.status.set(f'Connected • dashboard round trip {rtt:.0f} ms • monitoring costs no API tokens')
        rows = []
        for kind in ('download', 'transfer'):
            data = monitor.get(kind) or {}
            if not data: continue
            done, total = data.get('bytes_done', 0) or 0, data.get('bytes_total')
            pct = f'{100 * done / total:.1f}%' if total else 'size unknown'
            speed = (data.get('bytes_per_second', 0) or 0) / 1048576
            age = time.time() - data.get('updated', 0)
            state = data.get('state', 'unknown')
            if state in {'sending', 'downloading'} and age > 15: state += ' (stale; unconfirmed)'
            rows.append((kind + ': ' + str(data.get('filename', '')), state,
                f'{pct} · {done / 1048576:.2f} MiB · {speed:.2f} MiB/s', data.get('message', '')))
        for item in monitor.get('operations', []):
            rows.append((item.get('label', item.get('tool')), item.get('state'),
                'Not measurable' if item.get('state') == 'running' else '—', item.get('message', '')))
        for item in monitor.get('workflows', []):
            rows.append(('Workflow: ' + item.get('title', ''), item.get('state'),
                f'{item.get("completed_steps", 0)}/{item.get("total_steps", 0)} steps', item.get('report', '')))
        self.set_rows(self.tasks, rows)
        self.set_rows(self.agents, [(item.get('id') + (' ← ' + item['parent'] if item.get('parent') else ''),
            item.get('state', item.get('status')),
            str(item.get('progress', '')) + f' · estimate {item.get("spent", 0)}/{item.get("budget", 0)} tokens',
            item.get('report', '')[:600]) for item in monitor.get('agents', [])])
        metrics = monitor.get('metrics', {}); totals = metrics.get('totals', {}); latest = metrics.get('latest', {})
        d = totals.get('deepseek', {}); actual = totals.get('deepseek_reported', {}); q = totals.get('qwen_stt', {})
        qt = totals.get('qwen_tts', {})
        latency = latest.get('voice_latency', {})
        text = (f'Metered DeepSeek conversation/agent request attempts: {d.get("requests", 0):,}\n'
            f'Estimated input tokens: {d.get("estimated_input_tokens", 0):,}\n'
            f'Estimated output tokens: {d.get("estimated_output_tokens", 0):,}\n'
            f'Provider-reported tokens (only responses supplying usage): {json.dumps(actual)}\n\n'
            f'Qwen STT audio sent: {q.get("audio_seconds", 0):.1f} seconds\n'
            'Qwen audio token estimate: unavailable; audio seconds are measured instead.\n'
            f'Qwen fallback TTS characters: {qt.get("characters", 0):,}\n'
            f'Qwen TTS rough text-token estimate: {qt.get("estimated_text_tokens", 0):,} (not audio tokens)\n'
            'Edge TTS uses no Qwen tokens. Cached replies incur no new synthesis.\n\n'
            f'Dashboard request round trip: {rtt:.0f} ms (includes server handling, not voice latency)\n'
            f'Last measured voice stages: {json.dumps(latency, indent=2)}\n\n'
            + metrics.get('scope', '') + '\nCounters begin with this release. Estimates are not your invoice.\n'
            'Separate memory-consolidation and vision requests are not covered by these counters.')
        self.replace(self.metrics, text)
        audio = monitor.get('audio', {})
        self.replace(self.summary, f'Audio state: {audio.get("state", "unknown")}\n'
            f'Last heard: {audio.get("transcript", audio.get("heard", ""))}\n\n'
            f'Recorded tools: {len(monitor.get("operations", []))}\nSubagents: {len(monitor.get("agents", []))}\n\n'
            'Task percentages describe transferred bytes, not predicted completion.\n'
            'A transfer is complete only after the computer confirms its size and checksum.\n'
            'LAN discovery uses Find Pi; the remembered certificate identifies your Pi when its IP changes.')

    def load_settings(self):
        self.api('/api/settings', callback=self.render_settings)

    def render_settings(self, result):
        self.settings = result.get('settings', {}); self.setting_vars = {}
        for widget in self.settings_frame.winfo_children(): widget.destroy()
        for name, spec in self.settings.items():
            row = ttk.Frame(self.settings_frame, padding=8); row.pack(fill='x')
            ttk.Label(row, text=name, width=35).grid(row=0, column=0, sticky='w')
            var = tk.StringVar(value=str(spec['value']).lower() if isinstance(spec['value'], bool) else str(spec['value']))
            self.setting_vars[name] = var
            choices = spec.get('choices') or (['true', 'false'] if spec.get('value_type') == 'bool' else [])
            entry = ttk.Combobox(row, textvariable=var, values=choices, state='readonly', width=24) if choices else ttk.Entry(row, textvariable=var, width=26)
            entry.grid(row=0, column=1, padx=8)
            ttk.Button(row, text='Save', command=lambda n=name: self.save_setting(n)).grid(row=0, column=2)
            ttk.Button(row, text='Reset', command=lambda n=name: self.api('/api/settings', {'name': n, 'action': 'reset'}, lambda _: self.load_settings())).grid(row=0, column=3, padx=4)
            mode = 'Live' if spec.get('applies_live') else 'Restart required'
            ttk.Label(row, text=mode + ' • ' + spec.get('description', ''), wraplength=850).grid(row=1, column=0, columnspan=4, sticky='w', pady=4)

    def save_setting(self, name):
        value = self.setting_vars[name].get(); kind = self.settings[name]['value_type']
        try:
            if kind == 'int': value = int(value)
            elif kind == 'float': value = float(value)
            elif kind == 'bool': value = value == 'true'
        except ValueError: self.status.set('Enter a valid number.'); return
        self.api('/api/settings', {'name': name, 'value': value}, lambda r: self.status.set('Saved • ' + ('applies live' if r.get('applies_live') else 'restart ATHENA to apply')))

    def save_prompts(self):
        self.api('/api/prompts', {'system': self.system_prompt.get('1.0', 'end').strip(),
            'memory': self.memory_prompt.get('1.0', 'end').strip()})

    def send_chat(self):
        text = self.message.get().strip()
        if not text: return
        self.message.set(''); self.chat_log.insert('end', 'You: ' + text + '\n')
        self.api('/api/chat', {'text': text}, lambda r: self.chat_jobs.update({r['id']: text}))

    def chat_result(self, identity, result):
        if identity not in self.chat_jobs: return
        if result.get('status') == 'complete':
            del self.chat_jobs[identity]
            self.chat_log.insert('end', 'ATHENA: ' + result.get('answer', '') + '\n\n'); self.chat_log.see('end')

    def show_conversations(self, result):
        for row in reversed(result.get('conversations', [])):
            self.chat_log.insert('end', f'[{row.get("at", "")}]\nYou: {row.get("user", "")}\nATHENA: {row.get("assistant", "")}\n\n')
        self.chat_log.see('end')

    def close(self):
        self.closed = True; self.generation += 1
        self.client = None; self.password.set('')
        self.pool.shutdown(wait=False, cancel_futures=True); self.root.destroy()


def main():
    import sys
    root = tk.Tk()
    if '--self-test' in sys.argv: root.withdraw()
    app = App(root)
    if '--self-test' in sys.argv:
        app.render_settings({'settings': {
            'voice_test': {'value': True, 'value_type': 'bool', 'description': 'Test boolean', 'applies_live': True},
            'delay_test': {'value': 200, 'value_type': 'int', 'description': 'Test number', 'applies_live': False}}})
        app.show_monitor(({'operations': [{'tool': 'web_search', 'state': 'running'}],
            'agents': [{'id': 'child', 'parent': 'parent', 'state': 'running', 'progress': 'Reading sources'}],
            'download': {'state': 'downloading', 'bytes_done': 12, 'bytes_total': 100, 'updated': time.time()},
            'metrics': {'totals': {'qwen_stt': {'audio_seconds': 2.5}}, 'latest': {}}}, 12, {'serviceLabel': 'Test'}))
        root.update(); app.close(); return 0
    root.mainloop(); return 0


if __name__ == '__main__':
    raise SystemExit(main())
