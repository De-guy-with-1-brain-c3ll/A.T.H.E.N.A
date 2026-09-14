import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from athena.paths import data_directory, database_path
from athena.tools._sandbox import RUNNER, bubblewrap_command
from athena.tools.command import CommandTool


PROJECT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publisher = load('athena_pi_publisher', PROJECT / 'orange_pi' / 'pc' / 'publish_update.py')
updater = load('athena_pi_updater', PROJECT / 'orange_pi' / 'pi' / 'update_client.py')


class PiUpdateTests(unittest.TestCase):
    KEY = b'test-update-key-that-is-longer-than-thirty-two-bytes'

    def test_signed_release_contains_only_runtime_and_uses_manifest_version(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory) / 'feed'
            manifest = publisher.build_release(PROJECT, feed, 'test.20260902', self.KEY)
            disk_manifest = json.loads((feed / 'manifest.json').read_text())
            self.assertEqual(manifest, disk_manifest)
            bundle = (feed / manifest['archive']).read_bytes()
            updater.verify_release(manifest, bundle, self.KEY)
            extracted = Path(directory) / 'release'
            updater.extract_bundle(bundle, extracted)
            self.assertEqual((extracted / 'orange_pi' / 'VERSION').read_text().strip(),
                             manifest['version'])
            self.assertTrue((extracted / 'src' / 'athena' / 'feishu.py').is_file())
            self.assertTrue((extracted / 'src' / 'athena' / 'system' / 'system_prompt.txt').is_file())
            self.assertTrue((extracted / 'src' / 'athena' / 'system' / 'memory_prompt.txt').is_file())
            self.assertTrue((extracted / 'src' / 'athena' / 'web_static' / 'index.html').is_file())
            self.assertFalse((extracted / '.env').exists())
            self.assertFalse((extracted / 'data').exists())
            self.assertFalse((extracted / 'tests').exists())

    def test_tampered_release_or_signature_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory)
            manifest = publisher.build_release(PROJECT, feed, 'tamper-test', self.KEY)
            bundle = (feed / manifest['archive']).read_bytes()
            with self.assertRaises(ValueError):
                updater.verify_release(manifest, bundle + b'x', self.KEY)
            changed = dict(manifest, signature='0' * 64)
            with self.assertRaises(ValueError):
                updater.verify_release(changed, bundle, self.KEY)

    def test_unsafe_archive_path_is_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('../escape.py', 'bad')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                updater.extract_bundle(stream.getvalue(), root / 'release')
            self.assertFalse((root / 'escape.py').exists())

    def test_linux_sandbox_and_shell_are_restricted(self):
        command = bubblewrap_command('/usr/bin/bwrap', '/usr/bin/python3')
        for value in ('--unshare-all', '--die-with-parent', '--ro-bind', '--tmpfs', '-I'):
            self.assertIn(value, command)
        compile(RUNNER, 'sandbox_runner', 'exec')
        with patch('athena.tools.command.os.name', 'posix'):
            CommandTool._validate('echo hello', 'bash')
            with self.assertRaises(ValueError):
                CommandTool._validate('echo hello', 'powershell')
            with self.assertRaises(ValueError):
                CommandTool._validate('rm -rf /tmp/example', 'bash')
            with self.assertRaises(ValueError):
                CommandTool._validate('rm --recursive --force /tmp/example', 'bash')

    def test_pi_install_is_non_editable_and_uses_final_release_path(self):
        command = updater.pip_install_command(
            Path('/opt/athena/releases/v1/.venv/bin/python'),
            Path('/opt/athena'), Path('/opt/athena/releases/v1'))
        self.assertNotIn('-e', command)
        self.assertEqual(Path(command[-1]), Path('/opt/athena/releases/v1'))

    def test_service_units_are_hardened_and_use_one_interface(self):
        units = PROJECT / 'orange_pi' / 'systemd'
        feishu = (units / 'athena-feishu.service').read_text(encoding='utf-8')
        voice = (units / 'athena-voice.service').read_text(encoding='utf-8')
        dashboard = (units / 'athena-web.service').read_text(encoding='utf-8')
        update = (units / 'athena-update.service').read_text(encoding='utf-8')
        timer = (units / 'athena-update.timer').read_text(encoding='utf-8')
        for service in (feishu, voice):
            self.assertIn('User=athena', service)
            self.assertIn('EnvironmentFile=/etc/athena/athena.env', service)
            self.assertIn('MemoryMax=', service)
            self.assertIn('ReadWritePaths=/opt/athena/data', service)
        self.assertIn('/athena-feishu', feishu)
        self.assertNotIn('athena.main', feishu)
        self.assertIn('athena.main', voice)
        self.assertNotIn('athena.feishu', voice)
        self.assertIn('User=athena', dashboard)
        self.assertIn('EnvironmentFile=/etc/athena/web.env', dashboard)
        self.assertIn('ReadWritePaths=/opt/athena/data', dashboard)
        self.assertIn('/athena-web', dashboard)
        self.assertIn('User=root', update)
        self.assertIn('OnUnitActiveSec=2min', timer)

    def test_release_pruning_preserves_active_and_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / 'releases'
            current, previous, old = (releases / name for name in ('v3', 'v2', 'v1'))
            for path in (current, previous, old):
                path.mkdir(parents=True)
            updater.prune_releases(root, {current, previous})
            self.assertTrue(current.is_dir())
            self.assertTrue(previous.is_dir())
            self.assertFalse(old.exists())

    def test_pi_data_directory_can_live_outside_release(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'ATHENA_DATA_DIR': directory}, clear=False):
                self.assertEqual(data_directory(), Path(directory))
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop('ATHENA_DATABASE_PATH', None)
                    self.assertEqual(database_path(), Path(directory) / 'athena.db')


if __name__ == '__main__':
    unittest.main()
