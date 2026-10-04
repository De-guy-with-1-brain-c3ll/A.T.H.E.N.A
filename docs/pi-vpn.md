# Pi VPN

The desktop **ATHENA VPN.exe** imports FlClash/Clash/Mihomo YAML, including YAML
saved as `.txt`. Drop a file onto the executable icon, or use Browse. Enter the
Pi's address and root SSH password, then Apply config. The app never stores the
password or sends VPN credentials to DeepSeek. SSH host verification is required;
use a previously trusted Pi address (update its SSH known-host entry yourself if
its address changed).

Applying verifies the configuration with Mihomo before replacing it, starts the
service, and enables startup at boot. A service/controller startup failure restores
the previous configuration. Start/Stop/Refresh and Switch endpoint are in the app.
Dropping onto the executable icon is supported; dragging into the open window is
not supported—use Browse there.

ATHENA commands: "start the VPN", "stop the VPN", "VPN status", "list VPN
endpoints", "switch VPN endpoint to [exact name]". This controls **Pi traffic**,
not the separate browser running on your PC. Qwen/DashScope (`aliyuncs.com`,
`aliyun.com`, `alibabacloud.com`, `qwen.ai`, `qwenlm.ai`) and `deepseek.com` have
first-priority DIRECT rules. LAN traffic and SSH remain direct. DNS uses a mainland
resolver, redir-host mode and TLS/HTTP/QUIC domain sniffing; no global HTTP_PROXY
environment variables are set. Custom API hostnames require an additional direct
rule. Domain rules are not a claim that unrelated custom IP-only traffic bypasses
the VPN.

Imported global modes, rules, DNS, exposed controller ports, external dashboards,
and provider file paths are replaced with ATHENA's fixed policy. Standalone inline
nodes and HTTPS proxy subscriptions are supported. Chained nodes, local provider
files, and local key/certificate paths are rejected. A working account/server is
still necessary: installing Mihomo alone does not provide VPN connectivity.

The root-owned controller is `/usr/local/bin/athena-vpn-control`. ATHENA's Unix
account can only run its five fixed operations through a narrow sudoers grant.
It cannot import arbitrary config files or change the routing policy. The API
controller binds to loopback and requires a random token. Secrets stay in
root-only files under `/etc/athena-vpn`; do not commit exported configs.

Build: `python -m PyInstaller --clean --noconfirm --onefile --windowed --paths src
--name "ATHENA VPN" tools/pi_vpn_importer.py` (Python needs functioning Tcl/Tk).
Installer: `python tools/install_pi_vpn.py` verifies the official GitHub release's
SHA256 digest and installs the ARM64 binary, controller, service and sudoers.
