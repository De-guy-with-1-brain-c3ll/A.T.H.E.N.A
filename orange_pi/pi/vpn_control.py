"""Root-owned narrow VPN controller. Never accept shell commands or raw paths from the model."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request
import yaml
from vpn_config import compile_config

HOME = Path("/etc/athena-vpn")
STATE = Path("/var/lib/athena-vpn")
UNIT = "athena-vpn.service"

def system(action):
    return subprocess.run(["/usr/bin/systemctl", action, UNIT], capture_output=True, timeout=20)

def api(path, method="GET", data=None):
    token = (HOME/"token").read_text().strip()
    request = urllib.request.Request("http://127.0.0.1:9097" + path,
        data=json.dumps(data).encode() if data is not None else None, method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
        body = response.read(2_000_000)
        return json.loads(body) if body else {}

def status():
    result = {"installed": True, "configured": (HOME/"config.yaml").exists(),
              "running": system("is-active").returncode == 0,
              "direct_apis": ["Qwen", "DeepSeek"]}
    if result["running"]:
        try:
            group = api("/proxies/ATHENA_PROXY")
            result.update(endpoint=group.get("now"), endpoints=group.get("all", [])[:1000])
        except Exception:
            result["controller_ready"] = False
    return result

def select(name):
    group = api("/proxies/ATHENA_PROXY")
    if not isinstance(name, str) or name not in group.get("all", []):
        raise ValueError("Unknown endpoint. List endpoints first and use the exact name.")
    api("/proxies/ATHENA_PROXY", "PUT", {"name": name})
    return status()

def apply(path):
    source = Path(path)
    if source.parent != Path("/tmp") or not source.name.startswith("athena-vpn-upload-") or source.is_symlink():
        raise ValueError("Only a newly uploaded temporary VPN config is accepted.")
    if source.stat().st_uid != 0 or source.stat().st_size > 2_000_000:
        raise ValueError("Invalid config ownership or size.")
    config = compile_config(source.read_text(), (HOME/"token").read_text().strip())
    candidate = HOME/"candidate.yaml"
    candidate.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    candidate.chmod(0o600)
    verified = subprocess.run(["/usr/local/bin/mihomo", "-t", "-d", str(STATE), "-f", str(candidate)],
                               capture_output=True, timeout=40)
    if verified.returncode:
        candidate.unlink(missing_ok=True)
        raise ValueError("Mihomo rejected the config; the previous config was kept. Check your node settings.")
    target = HOME/"config.yaml"
    previous = target.read_bytes() if target.exists() else None
    was_running = status()["running"]
    os.replace(candidate, target)
    try:
        if system("restart").returncode:
            raise ValueError("VPN service could not start.")
        for attempt in range(15):
            time.sleep(.5)
            result = status()
            if result.get("endpoints"):
                endpoints = [name for name in result["endpoints"] if name != "DIRECT"]
                if not endpoints:
                    raise ValueError("Subscription returned no endpoints.")
                if result.get("endpoint") == "DIRECT":
                    result = select(endpoints[0])
                system("enable")
                return result
        raise ValueError("VPN controller did not become ready.")
    except Exception:
        system("stop")
        if previous is not None:
            target.write_bytes(previous); target.chmod(0o600)
            if was_running: system("start")
        else:
            target.unlink(missing_ok=True)
        raise
    finally:
        source.unlink(missing_ok=True)

def main():
    try:
        action = sys.argv[1]
        if action == "apply" and len(sys.argv) == 3:
            result = apply(sys.argv[2])
        elif action in {"status", "endpoints"} and len(sys.argv) == 2:
            result = status()
        elif action in {"start", "stop"} and len(sys.argv) == 2:
            if action == "start" and not (HOME/"config.yaml").exists():
                raise ValueError("Import your VPN YAML config using the desktop app first.")
            if system(action).returncode: raise ValueError("VPN service command failed.")
            if action == "start": time.sleep(.5)
            result = status()
        elif action == "select" and len(sys.argv) == 2:
            result = select(json.loads(sys.stdin.read(4096)).get("endpoint"))
        else:
            raise ValueError("Unsupported VPN action.")
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        # Never include imported secrets or raw validation output in model history.
        message = str(error) if isinstance(error, ValueError) else "VPN operation failed; check local service status."
        print(json.dumps({"error": message})); sys.exit(1)

if __name__ == "__main__": main()
