import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from athena.installation import defaults,decode_pairing,environment,pairing_code,save_config,read_config,certificate
from athena import voice_ipc

class InstallationTests(unittest.TestCase):
    def test_roundtrip_and_environment(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'ATHENA_APP_HOME':root}):
            config=defaults({'DEEPSEEK_API_KEY':'test-only','receiver_bind':'192.168.1.2'})
            save_config(config)
            self.assertEqual(read_config(),config)
            self.assertNotIn('receiver_bind',environment())
            self.assertEqual(environment()['ATHENA_WEB_HOST'],'127.0.0.1')
            self.assertTrue(Path(environment()['ATHENA_DATABASE_PATH']).is_absolute())
            self.assertEqual(defaults(config),config)

    def test_pairing_valid(self):
        pc={'role':'pc','url':'http://192.168.1.2:8781/upload','key':'a'*48}
        linux={'role':'linux','host':'10.0.0.2','fingerprint':'ab'*32,'password':'test-only'}
        for value in (pc,linux): self.assertEqual(decode_pairing(pairing_code(value),value['role']),value)

    def test_pairing_invalid(self):
        base={'role':'pc','url':'http://192.168.1.2:8781/upload','key':'a'*48}
        for url in ('http://8.8.8.8/upload','file:///upload','http://127.0.0.1/upload','http://user:pass@192.168.1.2/upload','http://[::1]/upload','http://192.168.1.2/private'):
            with self.subTest(url=url),self.assertRaises(ValueError):decode_pairing(pairing_code({**base,'url':url}),'pc')
        for value in ({'role':'linux','host':'192.168.1.2','fingerprint':'z'*64}, {'role':'pc','url':base['url'],'key':None}, ['broken']):
            with self.assertRaises(ValueError):decode_pairing(pairing_code(value),'pc' if not isinstance(value,dict) else value['role'])
        with self.assertRaises(ValueError):decode_pairing('ATHENA1.@@@','pc')
        with self.assertRaises(ValueError):decode_pairing(pairing_code(base),'linux')

    def test_certificate_identity_preserved(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'ATHENA_APP_HOME':root}):
            cert,key=certificate(); original=(cert.read_bytes(),key.read_bytes())
            self.assertEqual(certificate(),(cert,key))
            self.assertEqual(original,(cert.read_bytes(),key.read_bytes()))
            self.assertIn(b'PRIVATE KEY',original[1])

class WindowsIPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_authentication(self):
        class Coordinator:
            volume=37
        with patch.object(voice_ipc,'WINDOWS_HOST',True),patch.dict(os.environ,{'ATHENA_LOCAL_CONTROL_TOKEN':'test-only','ATHENA_LOCAL_CONTROL_PORT':'0'}):
            server=voice_ipc.VoiceControlServer(Coordinator())
            await server.start()
            port=server.server.sockets[0].getsockname()[1]
            try:
                for token,expected in [('wrong',False),('test-only',True)]:
                    reader,writer=await asyncio.open_connection('127.0.0.1',port)
                    writer.write(json.dumps({'action':'volume','token':token}).encode()+b'\n'); await writer.drain()
                    result=json.loads(await reader.readline()); self.assertEqual(result['ok'],expected)
                    writer.close(); await writer.wait_closed()
            finally:await server.close()

    async def test_audio_authentication(self):
        class Audio:
            attached=0
            def attach(self,sink):self.attached+=1
            def detach(self,sink):self.attached-=1
            def drain(self):pass
        audio=Audio()
        with patch.object(voice_ipc,'WINDOWS_HOST',True),patch.dict(os.environ,{'ATHENA_LOCAL_CONTROL_TOKEN':'test-only','ATHENA_LOCAL_AUDIO_PORT':'0'}):
            server=voice_ipc.VoiceAudioServer(audio); await server.start()
            port=server.server.sockets[0].getsockname()[1]
            try:
                reader,writer=await asyncio.open_connection('127.0.0.1',port)
                writer.write(voice_ipc.pack_frame(voice_ipc.CONTROL_FRAME,b'{"token":"wrong"}'));await writer.drain()
                self.assertEqual(await reader.read(),b'');self.assertEqual(audio.attached,0)
                writer.close();await writer.wait_closed()
                with patch.dict(os.environ,{'ATHENA_LOCAL_AUDIO_PORT':str(port)}):
                    reader,writer=await voice_ipc.open_audio_stream()
                    await asyncio.sleep(.02);self.assertEqual(audio.attached,1)
                    writer.close();await writer.wait_closed();await asyncio.sleep(.02)
                    self.assertEqual(audio.attached,0)
            finally:await server.close()
