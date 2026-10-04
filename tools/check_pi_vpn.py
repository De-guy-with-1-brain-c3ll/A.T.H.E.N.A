"""Check fixed routing in the real core without activating a dummy VPN."""
import subprocess
import tempfile
from pathlib import Path
import yaml
from athena.vpn_config import compile_config
from athena.services import build_registry

config = compile_config("proxies:\n- {name: test-node, type: ss, server: example.com, port: 443, cipher: aes-128-gcm, password: test-only}\n", "test-only")
with tempfile.TemporaryDirectory(prefix="athena-vpn-validation-") as directory:
    path = Path(directory)/"config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    result = subprocess.run(["/usr/local/bin/mihomo", "-t", "-d", directory, "-f", str(path)], capture_output=True, timeout=20)
    print("Actual Mihomo policy validation:", "passed" if result.returncode == 0 else "FAILED")
    if result.returncode: print(result.stdout.decode()[:1000]); raise SystemExit(1)
status = subprocess.run(["sudo", "-u", "athena", "sudo", "-n", "/usr/local/bin/athena-vpn-control", "status"], capture_output=True, timeout=10)
print("ATHENA-account controller:", status.stdout.decode().strip())
raise SystemExit(status.returncode)
