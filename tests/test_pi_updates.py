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

    def test_feed_discovery_requires_signature_and_recovers_changed_address(self):
        from urllib.parse import urlsplit
        with tempfile.TemporaryDirectory() as directory:
            manifest = publisher.build_release(PROJECT, Path(directory), 'discovery-test', self.KEY)
            def fetch(url, maximum, timeout=20):
                host = urlsplit(url).hostname
                if host == '192.168.33.186':
                    return json.dumps(manifest).encode()
                if host == '192.168.33.188':
                    return json.dumps(dict(manifest, signature='0' * 64)).encode()
                raise OSError('offline')
            with patch.object(updater, 'fetch', side_effect=fetch):
                url, found = updater.discover_feed('http://192.168.33.187:8765/', self.KEY)
            self.assertEqual(url, 'http://192.168.33.186:8765/')
            self.assertEqual(found['version'], 'discovery-test')
            with patch.object(updater, 'fetch', return_value=json.dumps(dict(manifest, signature='0' * 64)).encode()):
                with self.assertRaises(ConnectionError):
                    updater.discover_feed('http://192.168.33.187:8765/', self.KEY)

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
        for value in ('--unshare-all', '--die-with-parent', '--tmpfs', '-I'):
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

    def test_linux_sandbox_never_exposes_the_host_filesystem(self):
        """Coding runs without approval, so its sandbox must not see secrets."""
        def sources(command):
            return [command[index + 1] for index, item in enumerate(command)
                    if item == '--ro-bind']

        # On a host where the usual runtime paths exist, only those are bound.
        with patch('athena.tools._sandbox.os.path.exists',
                   lambda path: path in {'/usr', '/lib', '/etc/ld.so.cache'}), \
             patch('athena.tools._sandbox.os.path.isdir',
                   lambda path: path in {'/usr', '/lib'}), \
             patch('athena.tools._sandbox.os.path.isfile',
                   lambda path: path == '/etc/ld.so.cache'), \
             patch('athena.tools._sandbox.os.path.realpath', lambda path: path):
            command = bubblewrap_command('/usr/bin/bwrap', '/usr/bin/python3')
        self.assertEqual(sources(command), ['/usr', '/lib', '/etc/ld.so.cache'])

        # The whole host root must never be mounted, on any host.
        self.assertNotIn('/', sources(command))
        for secret in ('/etc/athena', '/opt/athena', '/root', '/home', '/var'):
            self.assertNotIn(secret, sources(command))

    def test_linux_sandbox_binds_the_resolved_path_for_symlinked_dirs(self):
        """Debian symlinks /lib and /bin into /usr; the visible name must exist."""
        with patch('athena.tools._sandbox.os.path.exists', lambda path: path == '/lib'), \
             patch('athena.tools._sandbox.os.path.isdir', lambda path: path == '/lib'), \
             patch('athena.tools._sandbox.os.path.realpath', lambda path: '/usr/lib'):
            command = bubblewrap_command('/usr/bin/bwrap', '/usr/bin/python3')
        index = command.index('--ro-bind')
        self.assertEqual(command[index + 1:index + 3], ['/usr/lib', '/lib'])

    def test_the_env_examples_define_each_setting_once(self):
        """A duplicated key silently overrides the earlier one at startup."""
        for name in ('.env.example', 'orange_pi/config/athena.env.example'):
            with self.subTest(file=name):
                text = (PROJECT / name).read_text(encoding='utf-8')
                keys = [line.split('=', 1)[0] for line in text.splitlines()
                        if line.strip() and not line.lstrip().startswith('#')]
                duplicates = sorted({key for key in keys if keys.count(key) > 1})
                self.assertEqual(duplicates, [], f"{name} repeats {duplicates}")

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


class SystemEnvironmentFileTests(unittest.TestCase):
    """A tool run by hand on the Pi must see what the services see.

    systemd injects /etc/athena/athena.env for the services. A command-line tool
    started from a shell has no such environment, so without this fallback every
    tool reported "not configured" on a fully configured Pi.
    """

    def _cleanup(self, name):
        import os
        os.environ.pop(name, None)

    def test_the_system_file_supplies_missing_settings(self):
        import os
        from unittest.mock import patch

        from athena import config

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "athena.env"
            path.write_text("# a comment\nATHENA_TEST_MARKER=from-the-system-file\n",
                            encoding="utf-8")
            self._cleanup("ATHENA_TEST_MARKER")
            with patch.object(config, "SYSTEM_ENV_FILE", path):
                config.load_local_environment()
                self.assertEqual(os.environ.get("ATHENA_TEST_MARKER"), "from-the-system-file")
            self._cleanup("ATHENA_TEST_MARKER")

    def test_an_existing_variable_is_never_overridden(self):
        import os
        from unittest.mock import patch

        from athena import config

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "athena.env"
            path.write_text("ATHENA_TEST_MARKER=from-the-file\n", encoding="utf-8")
            with patch.object(config, "SYSTEM_ENV_FILE", path):
                os.environ["ATHENA_TEST_MARKER"] = "from-the-environment"
                config.load_local_environment()
                self.assertEqual(os.environ["ATHENA_TEST_MARKER"], "from-the-environment")
            self._cleanup("ATHENA_TEST_MARKER")

    def test_a_missing_system_file_is_not_an_error(self):
        from unittest.mock import patch

        from athena import config

        with patch.object(config, "SYSTEM_ENV_FILE", Path("/nonexistent/athena.env")):
            config.load_local_environment()  # must not raise
