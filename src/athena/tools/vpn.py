"""Human-requested VPN lifecycle/endpoint controls, no configuration credentials in the model."""
import asyncio
import json
import os
from .models import ToolDefinition, ToolResult

class VPNTool:
    definition = ToolDefinition(name="manage_vpn",
        description="Manage the Orange Pi VPN when the user asks: status, start, stop, list endpoints, or select an exact listed endpoint. Qwen and DeepSeek always route directly. Does not install/import configs or reveal credentials. VPN changes affect Pi traffic, not the PC browser.",
        parameters={"type": "object", "properties": {"action": {"enum": ["status", "start", "stop", "endpoints", "select"]},
            "endpoint": {"type": "string", "maxLength": 200}}, "required": ["action"], "additionalProperties": False},
        timeout_seconds=30)

    async def execute(self, arguments):
        action = arguments.get("action")
        if action not in {"status", "start", "stop", "endpoints", "select"}:
            return ToolResult(False, "Unsupported VPN action.")
        if os.name == 'nt':
            return await self._windows(action,arguments.get('endpoint'))
        if os.name != "posix" or not os.path.isfile("/usr/local/bin/athena-vpn-control"):
            return ToolResult(False, "The Pi VPN controller is not installed on this machine.")
        socket_path = '/run/athena-vpn/control.sock'
        if os.path.exists(socket_path):
            reader, writer = await asyncio.open_unix_connection(socket_path, limit=65536)
            try:
                writer.write(json.dumps({'action': action, 'endpoint': arguments.get('endpoint')}).encode() + b'\n')
                await writer.drain()
                output = await asyncio.wait_for(reader.readline(), 28)
            finally:
                writer.close(); await writer.wait_closed()
            return self._result(action, output, 0)
        command = (["sudo", "-n"] if os.geteuid() != 0 else []) + ["/usr/local/bin/athena-vpn-control", action]
        process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps({"endpoint": arguments.get("endpoint")}).encode()), 25)
        except BaseException:
            if process.returncode is None: process.kill()
            await process.wait()
            raise
        return self._result(action, output, process.returncode)

    async def _windows(self,action,endpoint):
        import aiohttp
        from urllib.parse import urlsplit,quote
        address=os.environ.get('ATHENA_WINDOWS_VPN_URL','')
        url=urlsplit(address)
        if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost') or url.username or url.password:
            return ToolResult(False,'Set the loopback controller address and secret of your Clash/Mihomo Windows VPN in Athena setup, then open that VPN client.')
        headers={'Authorization':'Bearer '+os.environ.get('ATHENA_WINDOWS_VPN_SECRET','')}
        try:
            async with aiohttp.ClientSession(headers=headers,timeout=aiohttp.ClientTimeout(total=15)) as session:
                async def request(method,path,body=None):
                    async with session.request(method,address.rstrip('/')+path,json=body) as response:
                        response.raise_for_status()
                        return await response.json() if response.status!=204 else {}
                group=await request('GET','/proxies/ATHENA_PROXY')
                if action=='select':
                    if endpoint not in group.get('all',[]):return ToolResult(False,'Choose an exact endpoint from the listed endpoints.')
                    await request('PUT','/proxies/'+quote('ATHENA_PROXY',safe=''),{'name':endpoint})
                    group=await request('GET','/proxies/ATHENA_PROXY')
                if action in ('start','stop'):await request('PATCH','/configs',{'tun':{'enable':action=='start'}})
                config=await request('GET','/configs')
                result={'running':bool(config.get('tun',{}).get('enable')),'endpoint':group.get('now'),'endpoints':group.get('all',[])}
                answer=self._result(action,json.dumps(result).encode(),0)
                # The external Windows client may have changed its imported policy.
                return ToolResult(answer.success,answer.spoken_text.replace('. Qwen and DeepSeek bypass it.','.'),answer.data)
        except (aiohttp.ClientError,ValueError,OSError):return ToolResult(False,'Windows VPN controller is unavailable. Open your configured Mihomo VPN client and check its controller secret.')

    def _result(self, action, output, returncode):
        try: result = json.loads(output)
        except (ValueError, UnicodeError): return ToolResult(False, "VPN controller could not be reached.")
        if returncode or "error" in result:
            return ToolResult(False, result.get("error", "VPN operation failed."))
        if action == "endpoints":
            return ToolResult(True, "Endpoints: " + ", ".join(result.get("endpoints", [])[:40]), result)
        text = "VPN is running" if result["running"] else "VPN is stopped"
        if result.get("endpoint"): text += " using " + result["endpoint"]
        return ToolResult(True, text + ". Qwen and DeepSeek bypass it.", result)

def create_tools(): return [VPNTool()]
