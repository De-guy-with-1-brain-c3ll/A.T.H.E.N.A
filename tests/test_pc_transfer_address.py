import os
import socket
import unittest
import unittest.mock
from unittest.mock import patch

from athena import pc_transfer


class LanAddressTests(unittest.TestCase):
    """The address the receiver binds decides whether the Pi can reach it.

    On a machine with a proxy or a virtual switch installed, the default route
    answers with the tunnel's address rather than the Wi-Fi one. Binding that
    looks like success and then lets nothing in, which is how this went
    unnoticed for so long.
    """

    def bound(self, *addresses):
        return lambda *a, **k: [(socket.AF_INET, 1, 6, "", (address, 0))
                                 for address in addresses]

    def test_the_default_route_wins_when_it_is_a_real_adapter(self):
        interfaces = {"WLAN": ["192.168.33.186"]}
        with patch.object(pc_transfer, "_default_route_address", return_value="192.168.33.186"), \
             patch.object(pc_transfer, "socket") as fake, \
             patch("psutil.net_if_addrs", return_value=self._addrs(interfaces)):
            fake.getaddrinfo = self.bound("192.168.33.186", "127.0.0.1")
            self.assertEqual(pc_transfer.lan_address(), "192.168.33.186")

    def test_a_proxy_address_is_never_chosen(self):
        interfaces = {"WLAN": ["192.168.33.186"], "FlClash": ["28.0.0.1"]}
        with patch.object(pc_transfer, "_default_route_address", return_value="28.0.0.1"), \
             patch.object(pc_transfer, "socket") as fake, \
             patch("psutil.net_if_addrs", return_value=self._addrs(interfaces)):
            fake.getaddrinfo = self.bound("192.168.33.186", "28.0.0.1", "127.0.0.1")
            self.assertEqual(pc_transfer.lan_address(), "192.168.33.186")

    def test_a_hyper_v_switch_is_not_mistaken_for_ethernet(self):
        interfaces = {"vEthernet (WSL (Hyper-V firewall))": ["172.21.224.1"],
                      "WLAN": ["192.168.33.186"]}
        with patch.object(pc_transfer, "_default_route_address", return_value="172.21.224.1"), \
             patch.object(pc_transfer, "socket") as fake, \
             patch("psutil.net_if_addrs", return_value=self._addrs(interfaces)):
            fake.getaddrinfo = self.bound("172.21.224.1", "192.168.33.186", "127.0.0.1")
            self.assertEqual(pc_transfer.lan_address(), "192.168.33.186")

    def test_loopback_and_link_local_are_never_offered(self):
        with patch.object(pc_transfer, "_default_route_address", return_value=""), \
             patch.object(pc_transfer, "socket") as fake, \
             patch("psutil.net_if_addrs", return_value=self._addrs({})):
            fake.getaddrinfo = self.bound("127.0.0.1", "169.254.10.5", "192.168.33.186")
            self.assertEqual(pc_transfer.lan_address(), "192.168.33.186")

    def test_no_usable_address_is_an_explained_failure(self):
        with patch.object(pc_transfer, "_default_route_address", return_value=""), \
             patch.object(pc_transfer, "socket") as fake, \
             patch("psutil.net_if_addrs", return_value=self._addrs({})):
            fake.getaddrinfo = self.bound("127.0.0.1", "169.254.10.5")
            with self.assertRaisesRegex(RuntimeError, "same network"):
                pc_transfer.lan_address()

    @staticmethod
    def _addrs(interfaces):
        from types import SimpleNamespace
        return {name: [SimpleNamespace(address=address, family=socket.AF_INET)
                       for address in addresses]
                for name, addresses in interfaces.items()}


class BindAddressTests(unittest.TestCase):
    def bound(self, *addresses):
        return lambda *a, **k: [(socket.AF_INET, 1, 6, "", (address, 0))
                                 for address in addresses]

    def run_main(self, bind):
        argv = ["pc_transfer", "--bind", bind]
        fake = unittest.mock.MagicMock()
        fake.getaddrinfo = self.bound("192.168.33.186", "127.0.0.1")
        with patch("sys.argv", argv), \
             patch.dict(os.environ, {"ATHENA_PC_TRANSFER_KEY": "k" * 64}), \
             patch.object(pc_transfer, "lan_address", return_value="192.168.33.186"), \
             patch.dict(pc_transfer.__dict__, {"socket": fake}), \
             patch.object(pc_transfer, "web") as web:
            pc_transfer.main()
            return web.run_app.call_args

    def test_auto_resolves_to_the_detected_address(self):
        call = self.run_main("auto")
        self.assertEqual(call.kwargs["host"], "192.168.33.186")

    def test_an_explicit_current_address_is_served_as_given(self):
        call = self.run_main("192.168.33.186")
        self.assertEqual(call.kwargs["host"], "192.168.33.186")

    def test_a_public_address_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_main("8.8.8.8")