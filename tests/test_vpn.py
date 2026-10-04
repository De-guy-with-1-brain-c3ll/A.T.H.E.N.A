import unittest
from athena.vpn_config import compile_config, DIRECT_DOMAINS
from athena.tools.vpn import VPNTool

class VPNTests(unittest.IsolatedAsyncioTestCase):
    def test_direct_api_policy_overrides_imported_global_rules_and_lan_ports(self):
        cfg = compile_config('''proxies:
- {name: node, type: ss, server: example.com, port: 443, cipher: aes-128-gcm, password: secret}
mode: global
allow-lan: true
external-controller: 0.0.0.0:9090
rules: [MATCH,node]
''', "local-secret")
        self.assertEqual(cfg["mode"], "rule")
        self.assertFalse(cfg["allow-lan"])
        self.assertEqual(cfg["external-controller"], "127.0.0.1:9097")
        self.assertEqual(cfg["rules"][:len(DIRECT_DOMAINS)], [f"DOMAIN-SUFFIX,{d},DIRECT" for d in DIRECT_DOMAINS])
        self.assertEqual(cfg["rules"][-1], "MATCH,ATHENA_PROXY")
        self.assertIn("192.168.0.0/16", cfg["tun"]["route-exclude-address"])

    def test_config_must_have_nodes_and_uses_safe_yaml(self):
        for text in ("hello", "{}", "!!python/object/apply:os.system ['whoami']", "proxies: [not-a-node]"):
            with self.assertRaises(ValueError): compile_config(text, "secret")

    def test_provider_paths_and_rules_are_not_imported(self):
        cfg = compile_config('''proxy-providers:
  provider:
    type: http
    url: https://example.com/subscription
    path: /etc/passwd
external-ui: /etc
''', "secret")
        self.assertTrue(cfg["proxy-providers"]["provider"]["path"].startswith("providers/"))
        self.assertNotIn("external-ui", cfg)

    async def test_arbitrary_controller_actions_rejected(self):
        result = await VPNTool().execute({"action": "start; rm -rf /"})
        self.assertFalse(result.success)

