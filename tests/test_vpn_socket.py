import asyncio
import importlib.util
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

spec=importlib.util.spec_from_file_location('vpn_server_test',Path(__file__).parents[1]/'orange_pi/pi/vpn_server.py')
server=importlib.util.module_from_spec(spec)
with patch.dict('sys.modules', {'pwd': Mock()}):
    spec.loader.exec_module(server)

class VPNServerTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, payload, uid=1000):
        reader=asyncio.StreamReader(); reader.feed_data(json.dumps(payload).encode()+b'\n'); reader.feed_eof()
        writer=Mock()
        writer.get_extra_info.return_value.getsockopt.return_value=struct.pack('3i',123,uid,1000)
        writer.drain=AsyncMock(); writer.wait_closed=AsyncMock()
        process=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(b'{"running":false}',b'')))
        with patch.object(server.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=1000)), \
             patch.object(server.socket,'SO_PEERCRED',17,create=True), \
             patch.object(server.asyncio,'create_subprocess_exec',AsyncMock(return_value=process)) as spawn:
            await server.handle(reader,writer)
        return json.loads(writer.write.call_args.args[0]),spawn

    async def test_unknown_peer_cannot_execute_controller(self):
        result,spawn=await self.request({'action':'stop'},uid=2000)
        self.assertIn('Unauthorized',result['error']); spawn.assert_not_awaited()

    async def test_shell_commands_and_oversized_endpoint_are_rejected(self):
        for payload in ({'action':'stop; reboot'}, {'action':'select','endpoint':'x'*201},
                        {'action':'select','endpoint':['bad']}):
            result,spawn=await self.request(payload)
            self.assertIn('error',result); spawn.assert_not_awaited()

    async def test_authorized_stop_calls_only_fixed_controller(self):
        result,spawn=await self.request({'action':'stop','command':'reboot'})
        self.assertFalse(result['running'])
        self.assertEqual(spawn.call_args.args,('/usr/local/bin/athena-vpn-control','stop'))
