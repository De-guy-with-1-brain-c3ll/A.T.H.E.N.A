import asyncio
from pathlib import Path
import tempfile
import unittest

from athena.tools.command import CommandTool
from athena.tools.registry import ToolRegistry


class CommandRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = ToolRegistry()
        self.registry.register(CommandTool(Path(self.temp.name)))

    async def asyncTearDown(self):
        await self.registry.close()
        self.temp.cleanup()

    async def test_exact_command_requires_approval_then_runs_in_background(self):
        prompt = await self.registry.execute("run_command", {
            "shell": "cmd", "command": "echo ATHENA_COMMAND_OK"})
        self.assertTrue(prompt.data["approval_required"])
        self.assertIn("echo ATHENA_COMMAND_OK", prompt.spoken_text)
        self.assertEqual(self.registry.command_status().data["status"], "prepared")
        started = await self.registry.handle_user_command("yes")
        self.assertTrue(started.data["command_started"])
        await self.registry.wait_for_commands()
        status = self.registry.command_status()
        self.assertEqual(status.data["status"], "complete")
        self.assertIn("ATHENA_COMMAND_OK", status.spoken_text)
        self.assertFalse((await self.registry.handle_user_command("approve")).success)

    async def test_denial_and_other_request_never_execute(self):
        await self.registry.execute("run_command", {"shell": "cmd", "command": "echo no"})
        result = await self.registry.handle_user_command("no")
        self.assertIn("cancelled", result.spoken_text.casefold())
        self.assertEqual(self.registry.command_status().data["status"], "cancelled")
        await self.registry.execute("run_command", {"shell": "cmd", "command": "echo no"})
        await self.registry.handle_user_command("what time is it")
        self.assertFalse((await self.registry.handle_user_command("approve")).success)

    async def test_hidden_download_secret_and_destructive_commands_are_blocked(self):
        commands = [
            ("powershell", "powershell -EncodedCommand abc"),
            ("powershell", "Invoke-WebRequest https://example.com/a"),
            ("cmd", "type .env"),
            ("cmd", "shutdown /s"),
            ("powershell", "Remove-Item C:\\data -Recurse"),
        ]
        for shell, command in commands:
            result = await self.registry.execute("run_command", {"shell": shell, "command": command})
            self.assertFalse(result.success, command)
            self.assertFalse(result.data.get("approval_required", False), command)

    async def test_status_while_running_is_local_and_immediate(self):
        await self.registry.execute("run_command", {
            "shell": "powershell", "command": "Start-Sleep -Milliseconds 200"})
        await self.registry.handle_user_command("yes")
        status = await self.registry.handle_user_command("command status")
        self.assertEqual(status.data["status"], "running")
        await self.registry.wait_for_commands()

    async def test_desktop_is_an_explicit_approved_location(self):
        prompt = await self.registry.execute("run_command", {
            "shell": "cmd", "command": "echo desktop", "cwd": "desktop"})
        self.assertTrue(prompt.data["approval_required"])
        self.assertIn("Windows Desktop", prompt.spoken_text)

    async def test_approved_desktop_folder_and_text_file_are_really_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop = root / "Desktop"
            desktop.mkdir()
            registry = ToolRegistry()
            registry.register(CommandTool(root, desktop))
            command = ('New-Item -ItemType Directory -Force -Path "test" | Out-Null; '
                       'Set-Content -Path "test\\test.txt" -Value "ATHENA chat log" -Encoding UTF8')
            prompt = await registry.execute("run_command", {
                "shell": "powershell", "cwd": "desktop", "command": command})
            self.assertTrue(prompt.data["approval_required"])
            started = await registry.handle_user_command("yes")
            self.assertTrue(started.data["command_started"])
            await registry.wait_for_commands()
            self.assertEqual(registry.command_status().data["status"], "complete")
            self.assertIn("ATHENA chat log", (desktop / "test" / "test.txt").read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
