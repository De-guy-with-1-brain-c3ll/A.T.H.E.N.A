"""Drop a FlClash YAML/TXT on the executable, or Browse. Credentials never enter ATHENA chat."""
import json
import os
from pathlib import Path
import secrets
import shlex
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import paramiko
from athena.vpn_config import compile_config

def main():
    if "--self-test" in sys.argv:
        test_window = tk.Tk(); test_window.withdraw(); test_window.update(); test_window.destroy()
        return
    window = tk.Tk(); window.title("ATHENA Pi VPN"); window.geometry("620x450")
    root = Path.home()/"Desktop/VSCODE projects/ATHENA SOURCE"
    address = root/"orange_pi/pi-address.txt"
    host = tk.StringVar(value=address.read_text().strip() if address.exists() else "192.168.33.153")
    password = tk.StringVar()
    config = tk.StringVar(value=sys.argv[1] if len(sys.argv) > 1 else "")
    endpoint = tk.StringVar()
    result = tk.StringVar(value="Drop a FlClash .txt/.yaml onto this executable, or Browse.\nQwen + DeepSeek + LAN remain direct. Pi traffic only.")
    pane = ttk.Frame(window, padding=18); pane.pack(fill="both", expand=True)
    for label, variable, masked in (("Pi address", host, False), ("Pi root password", password, True), ("Config (.txt/.yaml)", config, False)):
        ttk.Label(pane, text=label).pack(anchor="w")
        ttk.Entry(pane, textvariable=variable, show="*" if masked else "").pack(fill="x", pady=(0, 5))
    ttk.Button(pane, text="Browse…", command=lambda: config.set(filedialog.askopenfilename(filetypes=[("FlClash config", "*.txt *.yaml *.yml"), ("All files", "*.*")]))).pack(anchor="w")
    choices = ttk.Combobox(pane, textvariable=endpoint, state="readonly"); choices.pack(fill="x", pady=8)
    buttons = ttk.Frame(pane); buttons.pack(fill="x")
    def run(action):
        # Tk variables are read on the UI thread, never by the worker.
        values = host.get().strip(), password.get(), config.get(), endpoint.get()
        for button in buttons.winfo_children(): button.configure(state="disabled")
        result.set("Connecting…")
        def worker():
            client = paramiko.SSHClient(); upload = None
            try:
                hostname, passwd, filename, selected = values
                from paramiko.hostkeys import HostKeyEntry, InvalidHostKey
                known = Path.home()/".ssh/known_hosts"
                for line in known.read_text().splitlines() if known.exists() else []:
                    try: entry = HostKeyEntry.from_line(line)
                    except (InvalidHostKey, ValueError): continue
                    if entry and entry.key:
                        for name in entry.hostnames: client.get_host_keys().add(name, entry.key.get_name(), entry.key)
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
                client.connect(hostname, username="root", password=passwd, timeout=30,
                    banner_timeout=30, look_for_keys=False, allow_agent=False)
                command = "/usr/local/bin/athena-vpn-control " + action
                if action == "apply":
                    source = Path(filename)
                    if not source.is_file() or source.stat().st_size > 2_000_000: raise ValueError("Select a config file under 2 MB.")
                    text = source.read_text(encoding="utf-8-sig")
                    compile_config(text, "validation-only")
                    upload = "/tmp/athena-vpn-upload-" + secrets.token_hex(16) + ".yaml"
                    sftp = client.open_sftp()
                    with sftp.open(upload, "w") as output: output.write(text)
                    sftp.chmod(upload, 0o600)
                    command += " " + shlex.quote(upload)
                stdin, stdout, stderr = client.exec_command(command, timeout=90)
                if action == "select": stdin.write(json.dumps({"endpoint": selected})); stdin.channel.shutdown_write()
                body = stdout.read(200_000).decode()
                try: data = json.loads(body)
                except ValueError: raise ValueError("Pi VPN controller is not installed or returned an invalid response.") from None
                if stdout.channel.recv_exit_status() or data.get("error"): raise ValueError(data.get("error", "VPN operation failed."))
                def finished():
                    choices["values"] = data.get("endpoints", [])
                    endpoint.set(data.get("endpoint", ""))
                    result.set(("VPN running" if data["running"] else "VPN stopped") + ". Qwen and DeepSeek stay direct.")
                window.after(0, finished)
            except Exception as error:
                message = str(error) if isinstance(error, (ValueError, paramiko.SSHException, OSError)) else "Import failed; check config and Pi connection."
                window.after(0, lambda msg=message: result.set(msg[:300]))
            finally:
                if upload and client.get_transport() and client.get_transport().is_active():
                    try: client.open_sftp().remove(upload)
                    except OSError: pass
                client.close()
                window.after(0, lambda: [button.configure(state="normal") for button in buttons.winfo_children()])
        threading.Thread(target=worker, daemon=True).start()
    for label, action in (("Apply config", "apply"), ("Start", "start"), ("Stop", "stop"), ("Refresh", "status"), ("Switch endpoint", "select")):
        ttk.Button(buttons, text=label, command=lambda a=action: run(a)).pack(side="left", padx=2)
    ttk.Label(pane, textvariable=result, wraplength=555).pack(anchor="w", pady=12)
    window.mainloop()

if __name__ == "__main__": main()
