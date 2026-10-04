"""Compile user VPN nodes into a fixed, API-direct Mihomo policy."""
import hashlib
from urllib.parse import urlsplit
import yaml

DIRECT_DOMAINS = ("deepseek.com", "aliyuncs.com", "aliyun.com", "alibabacloud.com", "qwen.ai", "qwenlm.ai")
LAN_RANGES = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")

def compile_config(text, secret):
    if len(text.encode()) > 2_000_000:
        raise ValueError("Config exceeds 2 MB.")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError("Invalid YAML config; check it in your VPN client.") from None
    if not isinstance(data, dict):
        raise ValueError("Use a Clash/Mihomo YAML config, not an OpenVPN or WireGuard file.")
    proxies = data.get("proxies", [])
    if not isinstance(proxies, list) or len(proxies) > 1000:
        raise ValueError("Invalid proxy list.")
    names = []
    for node in proxies:
        if not isinstance(node, dict) or not isinstance(node.get("name"), str) or not node.get("server") or not node.get("type"):
            raise ValueError("Each proxy needs name, server and type.")
        name = node["name"]
        if len(name) > 200 or name in names or name in {"DIRECT", "REJECT", "ATHENA_PROXY", "GLOBAL"}:
            raise ValueError("Invalid or duplicate proxy name.")
        # Imported nodes cannot delegate to groups that were deliberately removed.
        if node.get("dialer-proxy"):
            raise ValueError("Chained dialer-proxy configs are not supported; export standalone nodes.")
        if any(k in node for k in ("private-key-path", "certificate", "certificate-path")):
            raise ValueError("Export inline credentials rather than local certificate/key paths.")
        names.append(name)
    providers = {}
    source = data.get("proxy-providers", {})
    if not isinstance(source, dict) or len(source) > 30:
        raise ValueError("Invalid provider list.")
    for name, provider in source.items():
        if not isinstance(name, str) or not isinstance(provider, dict):
            raise ValueError("Invalid provider.")
        url = provider.get("url", "")
        if provider.get("type") != "http" or urlsplit(url).scheme != "https":
            raise ValueError("Providers must be HTTPS subscriptions; local provider paths are not imported.")
        providers[name] = {"type": "http", "url": url, "proxy": "DIRECT",
            "path": "providers/" + hashlib.sha256(name.encode()).hexdigest()[:20] + ".yaml",
            "interval": 3600, "health-check": {"enable": False}}
    if not names and not providers:
        raise ValueError("No VPN nodes or HTTPS subscriptions found in this config.")
    return {"mixed-port": 7897, "allow-lan": False, "bind-address": "127.0.0.1",
        "external-controller": "127.0.0.1:9097", "secret": secret,
        "mode": "rule", "log-level": "warning", "ipv6": False,
        "profile": {"store-selected": True}, "find-process-mode": "off",
        "tun": {"enable": True, "device": "athena-tun", "stack": "mixed",
            "auto-route": True, "auto-detect-interface": True,
            "dns-hijack": ["any:53"], "route-exclude-address": list(LAN_RANGES)},
        "dns": {"enable": True, "listen": "127.0.0.1:1053", "ipv6": False,
            "enhanced-mode": "redir-host", "default-nameserver": ["223.5.5.5", "119.29.29.29"],
            "nameserver": ["https://dns.alidns.com/dns-query"],
            "proxy-server-nameserver": ["223.5.5.5", "119.29.29.29"]},
        "sniffer": {"enable": True, "parse-pure-ip": True,
            "sniff": {"TLS": {"ports": [443, 8443]}, "HTTP": {"ports": [80]}, "QUIC": {"ports": [443]}}},
        "proxies": proxies, "proxy-providers": providers,
        "proxy-groups": [{"name": "ATHENA_PROXY", "type": "select",
            "proxies": names + ["DIRECT"], **({"use": list(providers)} if providers else {})}],
        "rules": [*(f"DOMAIN-SUFFIX,{domain},DIRECT" for domain in DIRECT_DOMAINS),
            *(f"IP-CIDR,{cidr},DIRECT,no-resolve" for cidr in LAN_RANGES),
            "IP-CIDR6,::1/128,DIRECT,no-resolve", "IP-CIDR6,fc00::/7,DIRECT,no-resolve",
            "MATCH,ATHENA_PROXY"]}
