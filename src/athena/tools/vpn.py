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
        if os.name != "posix" or not os.path.isfile("/usr/local/bin/athena-vpn-control"):
            return ToolResult(False, "The Pi VPN controller is not installed on this machine.")
        command = (["sudo", "-n"] if os.geteuid() != 0 else []) + ["/usr/local/bin/athena-vpn-control", action]
        process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps({"endpoint": arguments.get("endpoint")}).encode()), 25)
        except BaseException:
            if process.returncode is None: process.kill()
            await process.wait()
            raise
        try: result = json.loads(output)
        except (ValueError, UnicodeError): return ToolResult(False, "VPN controller could not be reached.")
        if process.returncode or "error" in result:
            return ToolResult(False, result.get("error", "VPN operation failed."))
        if action == "endpoints":
            return ToolResult(True, "Endpoints: " + ", ".join(result.get("endpoints", [])[:40]), result)
        text = "VPN is running" if result["running"] else "VPN is stopped"
        if result.get("endpoint"): text += " using " + result["endpoint"]
        return ToolResult(True, text + ". Qwen and DeepSeek bypass it.", result)

def create_tools(): return [VPNTool()]
