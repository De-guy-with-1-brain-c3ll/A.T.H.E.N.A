"""Packaged Windows applications and their explicitly selected worker modes."""
import argparse
import os
from pathlib import Path
import sys
import threading

def prepare():
    from athena.installation import environment,home,read_config
    if 'Companion' in Path(sys.executable).stem:
        os.environ.setdefault('ATHENA_APP_HOME',str(Path(os.environ.get('LOCALAPPDATA',Path.home()))/'ATHENA Companion'))
    os.environ.update(environment())
    binary=Path(getattr(sys,'_MEIPASS',Path(sys.executable).parent))/'bin'
    os.environ['PATH']=str(binary)+os.pathsep+os.environ.get('PATH','')
    for name in ('data','logs'): (home()/name).mkdir(parents=True,exist_ok=True)
    return read_config()

def worker(name):
    prepare()
    from athena.installation import home,read_config
    # Windowed executables have no console streams, including inherited handles.
    if sys.stdout is None: sys.stdout=(home()/'logs'/f'{name}.log').open('a',encoding='utf-8',buffering=1)
    if sys.stderr is None: sys.stderr=sys.stdout
    if name=='voice':
        from athena.main import main
        return main()
    if name=='web':
        from athena.installation import certificate
        cert,key=certificate(); os.environ.update(ATHENA_WEB_TLS_CERT=str(cert),ATHENA_WEB_TLS_KEY=str(key))
        from athena.web import main
        sys.argv=[sys.argv[0]]
        return main()
    if name=='inbox':
        from athena.pc_transfer import main
        from athena.installation import home,read_config
        settings=read_config()
        bind='auto' if settings.get('receiver_auto') else settings.get('receiver_bind','127.0.0.1')
        sys.argv=[sys.argv[0],'--bind',bind,'--port',str(read_config().get('inbox_port',8781)),'--inbox',str(home()/'Inbox')]
        main(); return 0
    if name=='feishu':
        from athena.feishu import main
        return main()
    if name=='teams':
        from athena.tools.teams import login
        login(); return 0
    if name=='yt-dlp':
        import yt_dlp
        yt_dlp.main(sys.argv[3:]); return 0
    if name=='restart-web':
        from athena.local_service import control
        import time
        time.sleep(1.5); control('restart','web'); return 0
    raise ValueError('Unknown application component.')

def self_test():
    import json,subprocess
    from athena.tts.edge import decoder_binary
    from athena.settings.store import RuntimeSettingsStore
    from athena.tools.registry import ToolRegistry
    from athena import web
    import tkinter as tk
    root=tk.Tk(); root.withdraw(); root.update(); root.destroy()
    registry=ToolRegistry.discover(services={'settings':RuntimeSettingsStore()})
    required={'get_weather','set_alarm','coding_workspace','upload_to_pc','teams_assignments','youtube_audio','netease_music','pc_browser'}
    assert required.issubset(set(registry.names()))
    assert (web.STATIC/'index.html').is_file()
    binary=decoder_binary(); assert binary
    subprocess.run([binary,'-version'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    report=json.dumps({'ok':True,'tools':len(registry.names()),'ffmpeg':True,'gui':True,'speaker_used':False})
    if '--report' in sys.argv: Path(sys.argv[sys.argv.index('--report')+1]).write_text(report)
    elif sys.stdout: print(report)
    return 0

def gui(companion=False):
    import tkinter as tk
    from tkinter import ttk,messagebox,simpledialog
    import webbrowser
    from athena.installation import defaults,read_config,save_config,home,pairing_code,decode_pairing
    from athena import local_service
    root=tk.Tk(); root.title('ATHENA Companion' if companion else 'ATHENA'); root.geometry('640x650')
    canvas=tk.Canvas(root,highlightthickness=0)
    scrollbar=ttk.Scrollbar(root,orient='vertical',command=canvas.yview)
    scrollbar.pack(side='right',fill='y'); canvas.pack(side='left',fill='both',expand=True)
    canvas.configure(yscrollcommand=scrollbar.set)
    pane=ttk.Frame(canvas,padding=24); item=canvas.create_window((0,0),window=pane,anchor='nw')
    pane.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>',lambda event:canvas.itemconfigure(item,width=event.width))
    canvas.bind_all('<MouseWheel>',lambda event:canvas.yview_scroll(-int(event.delta/120),'units'))
    ttk.Label(pane,text='A T H E N A',font=('Segoe UI',24,'bold')).pack(anchor='w')
    status=tk.StringVar(value='Set up once, then start when you are ready.')
    ttk.Label(pane,textvariable=status,wraplength=570).pack(anchor='w',pady=12)
    config=defaults(read_config()); save_config(config)
    def background(fn):
        def run():
            try: result=fn(); root.after(0,lambda:status.set(str(result or 'Ready.')))
            except Exception as error:
                text=str(error); root.after(0,lambda:messagebox.showerror('ATHENA',text))
        threading.Thread(target=run,daemon=True).start()
    if companion:
        config['receiver_auto']=True; save_config(config)
        ttk.Label(pane,text='1. Create your PC code and paste it into the Linux installer.\n2. Paste the device code shown when Linux setup finishes.',wraplength=570).pack(anchor='w',pady=15)
        def pc_code():
            from athena.pc_transfer import lan_address
            address=lan_address(); config['receiver_bind']=address; save_config(config)
            prepare(); local_service.control('restart','inbox')
            code=pairing_code({'role':'pc','url':f'http://{address}:8781/upload','key':config['ATHENA_PC_TRANSFER_KEY']})
            root.clipboard_clear(); root.clipboard_append(code)
            status.set('PC code copied. Paste it into the Linux setup. Keep this code private.')
        def connect():
            code=simpledialog.askstring('Pair your Linux device','Paste the device pairing code from the Linux installer:')
            if not code:return
            try:
                value=decode_pairing(code.strip(),'linux')
                from athena.desktop import App,save_preferences
                save_preferences(value['host'],value['fingerprint'])
                window=tk.Toplevel(root); app=App(window)
                app.password.set(value.get('password','')); app.connect()
            except Exception as error: messagebox.showerror('Pairing',str(error))
        ttk.Button(pane,text='Copy PC pairing code',command=pc_code).pack(fill='x',pady=6)
        def firewall():
            import base64,ctypes
            executable=str(Path(sys.executable)).replace("'","''")
            script=f"New-NetFirewallRule -DisplayName 'ATHENA Companion receiver' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8781 -Profile Private -RemoteAddress LocalSubnet -Program '{executable}'"
            encoded=base64.b64encode(script.encode('utf-16-le')).decode()
            result=ctypes.windll.shell32.ShellExecuteW(None,'runas','powershell.exe','-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand '+encoded,None,0)
            if result<=32:messagebox.showerror('Network access','Windows did not approve the firewall change.')
            else:status.set('Approve the Windows administrator prompt to allow files from your private local network.')
        ttk.Button(pane,text='Allow file receiving on private networks',command=firewall).pack(fill='x',pady=6)
        ttk.Button(pane,text='Pair Linux device',command=connect).pack(fill='x',pady=6)
        def existing():
            from athena.desktop import App
            App(tk.Toplevel(root))
        ttk.Button(pane,text='Open an already paired device',command=existing).pack(fill='x',pady=6)
        ttk.Label(pane,text='Windows may ask you to allow network access. Choose private networks for your home connection.',wraplength=560).pack(anchor='w',pady=12)
        # Existing receivers are resumed; no microphone or speaker is opened.
        if config.get('receiver_bind'): background(lambda:local_service.start('inbox'))
    else:
        ttk.Label(pane,text='Athena runs on this PC. AI and speech recognition use your accounts. Nothing listens until you click Start listening.',wraplength=560).pack(anchor='w',pady=8)
        fields={}
        for label,name,secret in [('DeepSeek API key','DEEPSEEK_API_KEY',True),('DashScope speech API key','DASHSCOPE_API_KEY',True),('Timezone (example Europe/London or Asia/Shanghai)','ATHENA_TIMEZONE',False),('Microsoft Teams application ID (optional)','MICROSOFT_CLIENT_ID',False),('Tavily search key (optional)','TAVILY_API_KEY',True),('Feishu application ID (optional)','FEISHU_APP_ID',False),('Feishu application secret (optional)','FEISHU_APP_SECRET',True),('Windows VPN controller URL (optional; example http://127.0.0.1:9097)','ATHENA_WINDOWS_VPN_URL',False),('Windows VPN controller secret (optional)','ATHENA_WINDOWS_VPN_SECRET',True),('Dashboard password','ATHENA_WEB_PASSWORD',True)]:
            ttk.Label(pane,text=label).pack(anchor='w'); var=tk.StringVar(value=config.get(name,'')); fields[name]=var
            ttk.Entry(pane,textvariable=var,show='*' if secret else '').pack(fill='x',pady=(2,6))
        def save():
            from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
            try:ZoneInfo(fields['ATHENA_TIMEZONE'].get().strip())
            except (ZoneInfoNotFoundError,ValueError):raise ValueError('Enter a timezone such as Europe/London, Asia/Shanghai or UTC.') from None
            config.update({name:var.get().strip() for name,var in fields.items()}); save_config(config); prepare()
            return 'Settings saved. History is stored on this PC.'
        def start():
            save()
            if not config.get('DEEPSEEK_API_KEY') or not config.get('DASHSCOPE_API_KEY'): raise ValueError('Add both API keys before starting voice.')
            for component in ('inbox','web','voice'): local_service.start(component)
            return 'Starting ATHENA. Open the dashboard for chat, tools, and settings.'
        ttk.Button(pane,text='Save settings',command=lambda:status.set(save())).pack(fill='x',pady=3)
        ttk.Button(pane,text='Start listening',command=lambda:background(start)).pack(fill='x',pady=3)
        ttk.Button(pane,text='Stop listening',command=lambda:background(lambda:local_service.stop('voice'))).pack(fill='x',pady=3)
        def dashboard():
            save(); local_service.start('web'); webbrowser.open('https://127.0.0.1:8780')
        ttk.Button(pane,text='Open dashboard',command=dashboard).pack(fill='x',pady=3)
        def teams():
            save()
            if not config.get('MICROSOFT_CLIENT_ID'): raise ValueError('Add your Microsoft application ID first.')
            local_service.start('teams')
            return 'Sign-in instructions are in logs/teams.log. Open saved files and logs below, then open teams.log.'
        ttk.Button(pane,text='Sign in to Teams',command=lambda:background(teams)).pack(fill='x',pady=3)
        ttk.Button(pane,text='Start Feishu integration',command=lambda:background(lambda:(save(),local_service.start('feishu'),'Feishu started.'))).pack(fill='x',pady=3)
        def coding():
            import shutil,subprocess
            executable=shutil.which('docker')
            if not executable:
                webbrowser.open('https://docs.docker.com/desktop/setup/install/windows-install/')
                return 'Install Docker Desktop using the opened guide, open Docker Desktop, then click this button again.'
            result=subprocess.run([executable,'pull','python:3.12-slim'],capture_output=True,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode: raise ValueError('Open Docker Desktop and wait until its engine is running, then retry. '+result.stderr[-400:])
            return 'Safe code execution is ready.'
        ttk.Button(pane,text='Set up safe code execution',command=lambda:background(coding)).pack(fill='x',pady=3)
        def vpn_export():
            from tkinter import filedialog
            import yaml,secrets
            from athena.vpn_config import compile_config
            source=filedialog.askopenfilename(title='Select your exported Clash/Mihomo VPN configuration')
            if not source:return
            try:
                secret=secrets.token_urlsafe(32)
                target=home()/'athena-vpn.yaml'
                target.write_text(yaml.safe_dump(compile_config(Path(source).read_text(encoding='utf-8-sig'),secret)),encoding='utf-8')
                fields['ATHENA_WINDOWS_VPN_URL'].set('http://127.0.0.1:9097')
                fields['ATHENA_WINDOWS_VPN_SECRET'].set(secret); save()
                os.startfile(home())
                messagebox.showinfo('Windows VPN','Import athena-vpn.yaml into your Mihomo/Clash Windows client and start its core. Athena can then toggle TUN and select endpoints. The exported file contains private VPN credentials.')
            except Exception as error:messagebox.showerror('Windows VPN',str(error))
        ttk.Button(pane,text='Prepare Windows VPN configuration',command=vpn_export).pack(fill='x',pady=3)
    ttk.Button(pane,text='Open saved files and logs',command=lambda:os.startfile(home())).pack(fill='x',pady=10)
    root.mainloop(); return 0

def main():
    if '--worker' in sys.argv: return worker(sys.argv[sys.argv.index('--worker')+1])
    prepare()
    if '--self-test' in sys.argv:return self_test()
    if '--stop-all' in sys.argv:
        from athena.local_service import stop
        for name in ('voice','web','inbox','feishu','teams'):stop(name)
        return 0
    return gui('--companion' in sys.argv or 'Companion' in Path(sys.executable).stem)

if __name__=='__main__': raise SystemExit(main())
