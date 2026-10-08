"""Root-owned narrow VPN socket: only installed controller actions, never shell text."""
import asyncio
import json
import os
from pathlib import Path
import pwd
import socket
import struct

PATH=Path('/run/athena-vpn/control.sock')
ALLOWED={'status','start','stop','endpoints','select'}

async def handle(reader,writer):
    try:
        _,uid,_=struct.unpack('3i',writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if uid not in (0,pwd.getpwnam('athena').pw_uid): raise ValueError('Unauthorized peer')
        raw=await asyncio.wait_for(reader.readline(),3)
        if len(raw)>4096: raise ValueError('Request too large')
        request=json.loads(raw); action=request.get('action')
        if action not in ALLOWED: raise ValueError('Unsupported VPN action')
        endpoint=request.get('endpoint')
        if endpoint is not None and (not isinstance(endpoint,str) or len(endpoint)>200): raise ValueError('Invalid endpoint')
        process=await asyncio.create_subprocess_exec('/usr/local/bin/athena-vpn-control',action,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            output,_=await asyncio.wait_for(process.communicate(json.dumps({'endpoint':endpoint}).encode()),25)
        finally:
            if process.returncode is None: process.kill(); await process.wait()
        result=json.loads(output)
    except Exception as error:
        result={'error':str(error)[:250]}
    writer.write(json.dumps(result).encode()+b'\n')
    await writer.drain(); writer.close(); await writer.wait_closed()

async def main():
    PATH.unlink(missing_ok=True)
    server=await asyncio.start_unix_server(handle,str(PATH),limit=4096)
    os.chown(PATH,0,pwd.getpwnam('athena').pw_gid); PATH.chmod(0o660)
    async with server: await server.serve_forever()

if __name__ == '__main__':
    asyncio.run(main())
