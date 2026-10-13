from pathlib import Path
import subprocess
import tempfile
import unittest
from athena.self_update import install, rollback


class UpdateTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_install_immutable_reviewed_files_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, candidate = root / 'live', root / 'candidate'
            (source / 'src').mkdir(parents=True)
            (candidate / 'src').mkdir(parents=True)
            (source / 'src/value.py').write_text('VALUE = 1\n')
            (candidate / 'src/value.py').write_text('VALUE = 1\n')
            self.git(candidate, 'init')
            self.git(candidate, 'add', 'src')
            baseline = self.git(candidate, 'write-tree')
            (candidate / 'src/value.py').write_text('VALUE = 2\n')
            self.git(candidate, 'add', 'src')
            verified = self.git(candidate, 'write-tree')
            backup = root / 'backup'
            install(source, candidate, baseline, backup, verified)
            self.assertEqual((source / 'src/value.py').read_text(), 'VALUE = 2\n')
            rollback(source, backup)
            self.assertEqual((source / 'src/value.py').read_text(), 'VALUE = 1\n')

    def test_modified_worktree_is_not_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, candidate = root / 'live', root / 'candidate'
            (source / 'src').mkdir(parents=True)
            (candidate / 'src').mkdir(parents=True)
            (source / 'src/value.py').write_text('VALUE = 1\n')
            (candidate / 'src/value.py').write_text('VALUE = 1\n')
            self.git(candidate, 'init'); self.git(candidate, 'add', 'src')
            baseline = self.git(candidate, 'write-tree')
            (candidate / 'src/value.py').write_text('VALUE = 2\n')
            self.git(candidate, 'add', 'src'); verified = self.git(candidate, 'write-tree')
            (candidate / 'src/value.py').write_text('VALUE = 3\n')
            with self.assertRaisesRegex(ValueError, 'changed after staging'):
                install(source, candidate, baseline, root / 'backup', verified)
            self.assertEqual((source / 'src/value.py').read_text(), 'VALUE = 1\n')
