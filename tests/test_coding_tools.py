import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from athena.tools.coding import CodingWorkspaceTool
from athena.tools._sandbox import docker_command, run_snapshot, RUNNER


class CodingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runner = AsyncMock(return_value={"success": True, "exit_code": 0, "stdout": "OK"})
        self.tool = CodingWorkspaceTool(Path(self.temp.name) / "coding", self.runner)
        await self.tool.execute({"action": "create", "project": "demo"})

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_implements_reads_and_runs_snapshot(self):
        result = await self.tool.execute({"action": "write", "project": "demo", "path": "main.py", "content": "print('hello')\n"})
        self.assertTrue(result.success)
        self.assertFalse(result.data["tested"])
        read = await self.tool.execute({"action": "read", "project": "demo", "path": "main.py"})
        self.assertIn("hello", read.data["content"])
        ran = await self.tool.execute({"action": "run", "project": "demo"})
        self.assertTrue(ran.success)
        self.assertEqual(self.runner.call_args.args[0], {"main.py": "print('hello')\n"})

    async def test_failing_tests_are_reported_as_failure(self):
        self.runner.return_value = {"success": False, "exit_code": 1, "stderr": "AssertionError"}
        result = await self.tool.execute({"action": "test", "project": "demo"})
        self.assertFalse(result.success)
        self.assertEqual(result.data["exit_code"], 1)

    async def test_rejects_traversal_secrets_absolute_and_reserved_paths(self):
        for path in ["../escape.py", "/escape.py", "C:/escape.py", ".env", "sub/.env", "NUL.py", "x:stream.py", "a//b.py"]:
            result = await self.tool.execute({"action": "write", "project": "demo", "path": path, "content": "bad"})
            self.assertFalse(result.success, path)
        self.assertFalse((await self.tool.execute({"action": "create", "project": "../escape"})).success)

    async def test_rejects_oversized_file(self):
        result = await self.tool.execute({"action": "write", "project": "demo", "path": "main.py", "content": "x" * 60001})
        self.assertFalse(result.success)

    async def test_no_host_fallback_without_docker(self):
        with patch("athena.tools._sandbox.shutil.which", return_value=None):
            result = await run_snapshot({"main.py": "print('test')"}, "run")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "sandbox_unavailable")

    def test_docker_command_enforces_isolation(self):
        args = docker_command("docker", "athena-job-test")
        for flag in ["--network=none", "--read-only", "--cap-drop=ALL", "--memory=256m", "--pids-limit=64", "--user=65534:65534", "--pull=never"]:
            self.assertIn(flag, args)
        self.assertNotIn("--mount", args)
        self.assertNotIn("-v", args)
        compile(RUNNER, "container_runner", "exec")

    async def test_symlinks_rejected(self):
        project = self.tool.project_path("demo")
        target = Path(self.temp.name) / "external.py"
        target.write_text("SECRET", encoding="utf-8")
        try:
            (project / "link.py").symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation is not permitted on this Windows account")
        result = await self.tool.execute({"action": "read", "project": "demo", "path": "link.py"})
        self.assertFalse(result.success)


@unittest.skipUnless(os.environ.get("ATHENA_TEST_DOCKER") == "1", "Set ATHENA_TEST_DOCKER=1 for real isolated execution")
class DockerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pass_failure_no_tests_and_timeout(self):
        files = {"main.py": "def add(a,b): return a+b\n", "test_main.py": "import unittest\nfrom main import add\nclass TestAdd(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n"}
        self.assertTrue((await run_snapshot(files, "test"))["success"])
        files["main.py"] = "def add(a,b): return 0\n"
        self.assertFalse((await run_snapshot(files, "test"))["success"])
        self.assertFalse((await run_snapshot({"main.py": "pass"}, "test"))["success"])
        result = await run_snapshot({"main.py": "while True: pass"}, "run", timeout=2)
        self.assertTrue(result["timed_out"])
