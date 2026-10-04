"""Install an SSH-staged signed bundle using the normal rollback-capable updater."""
import importlib.util
import json
import os
from pathlib import Path
import sys

stage = Path(sys.argv[1]).resolve()
for line in Path('/etc/athena/update.env').read_text().splitlines():
    if line.strip() and not line.lstrip().startswith('#') and '=' in line:
        name, value = line.split('=', 1)
        os.environ[name.strip()] = value.strip().strip('\"\'')
spec = importlib.util.spec_from_file_location('updater', stage/'update_client.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
manifest = json.loads((stage/'manifest.json').read_text())
bundle = (stage/manifest['archive']).read_bytes()
root = Path('/opt/athena')
with updater.update_lock(root):
    updater.verify_release(manifest, bundle, os.environ['ATHENA_UPDATE_KEY'].encode())
    if int(manifest['sequence']) <= int(updater.installed_state(root)['sequence']):
        raise RuntimeError('Refusing an older release sequence')
    updater.install_release(root, manifest, bundle, 'athena-voice.service')
    updater.restart_service('athena-feishu.service')
print('Installed and restarted ATHENA ' + manifest['version'], flush=True)
