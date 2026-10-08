"""Build clean distributable installers without local keys, models or saved data."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'dist/installers'

def linux():
    OUT.mkdir(parents=True,exist_ok=True)
    selected=[ROOT/'pyproject.toml',ROOT/'.env.example',ROOT/'docs/installation.md']
    for directory in ('src/athena','orange_pi/pi','orange_pi/systemd','orange_pi/config','packaging/linux'):
        selected.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo') and not p.name.startswith('.'))
    selected.append(ROOT/'orange_pi/VERSION')
    payload=io.BytesIO()
    with tarfile.open(fileobj=payload,mode='w:gz') as bundle:
        for path in sorted(set(selected)):
            info=tarfile.TarInfo('ATHENA/'+path.relative_to(ROOT).as_posix()); data=path.read_bytes()
            # Windows Git line endings must not make shell interpreters fail.
            if path.suffix in ('.sh','.service','.timer','.py','.toml','.example','.md'):data=data.replace(b'\r\n',b'\n')
            info.size=len(data); info.mode=0o755 if path.suffix=='.sh' else 0o644
            bundle.addfile(info,io.BytesIO(data))
    data=payload.getvalue(); digest=hashlib.sha256(data).hexdigest()
    header=f'''#!/usr/bin/env bash
set -euo pipefail
line=$(awk '/^__ATHENA_PAYLOAD__$/ {{print NR+1; exit}}' "$0")
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
tail -n +"$line" "$0" > "$stage/package.tar.gz"
printf '%s  %s\\n' '{digest}' "$stage/package.tar.gz" | sha256sum --check --status
tar -xzf "$stage/package.tar.gz" -C "$stage"
bash "$stage/ATHENA/packaging/linux/install.sh" "$@"
exit
__ATHENA_PAYLOAD__
'''.encode()
    target=OUT/'ATHENA-Linux-Setup.run'; target.write_bytes(header+data); target.chmod(0o755)
    print('Built',target.name,len(header+data),'bytes')

def windows(iscc):
    import imageio_ffmpeg
    compiler=Path(iscc) if iscc else None
    if not compiler or not compiler.is_file():raise RuntimeError('Supply --iscc with the Inno Setup ISCC.exe compiler path.')
    stage=ROOT/'outputs/packaging'; stage.mkdir(parents=True,exist_ok=True)
    decoder=Path(imageio_ffmpeg.get_ffmpeg_exe())
    prepared=stage/'bin/ffmpeg.exe'; prepared.parent.mkdir(exist_ok=True)
    shutil.copy2(decoder,prepared)
    for variant in ('Standalone','Companion'):
        label='ATHENA '+variant
        if variant=='Companion':
            destination=ROOT/'dist'/label
            if destination.exists():shutil.rmtree(destination)
            shutil.copytree(ROOT/'dist/ATHENA Standalone',destination)
            (destination/'ATHENA Standalone.exe').rename(destination/'ATHENA Companion.exe')
            subprocess.run([str(compiler),'/DVariant='+variant,str(ROOT/'packaging/windows.iss')],check=True,cwd=ROOT)
            continue
        spec=stage/(variant+'.spec')
        spec.write_text(f'''from PyInstaller.utils.hooks import collect_submodules, collect_data_files
a=Analysis([{str(ROOT/'tools/athena_app.py')!r}],pathex=[{str(ROOT/'src')!r}],
 binaries=[({str(prepared)!r},'bin')],
 datas=collect_data_files('athena')+collect_data_files('imageio_ffmpeg')+collect_data_files('playwright')+collect_data_files('yt_dlp'),
 hiddenimports=collect_submodules('athena')+collect_submodules('yt_dlp')+['psutil','cryptography','pyaudio','websocket'],
 excludes=['torch','tensorflow','matplotlib','tests'],noarchive=False)
# The imageio hook also collects its original decoder; ship just one copy.
a.binaries=[x for x in a.binaries if 'ffmpeg' not in x[0].lower() or x[0].replace('\\\\','/')=='bin/ffmpeg.exe']
a.datas=[x for x in a.datas if not (x[0].lower().endswith('.exe') and 'ffmpeg' in x[0].lower())]
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name={label!r},console=False)
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name={label!r})
''',encoding='utf-8')
        subprocess.run([sys.executable,'-m','PyInstaller','--clean','--noconfirm','--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'build/installers'),str(spec)],check=True,cwd=ROOT)
        # FFmpeg callers use its standard executable name.
        bin_dir=ROOT/'dist'/label/'_internal/bin'; bin_dir.mkdir(parents=True,exist_ok=True)
        shutil.copy2(decoder,bin_dir/'ffmpeg.exe')
        subprocess.run([str(compiler),'/DVariant='+variant,str(ROOT/'packaging/windows.iss')],check=True,cwd=ROOT)

def manifest():
    files=sorted(p for p in OUT.iterdir() if p.suffix in ('.exe','.run'))
    (OUT/'SHA256SUMS.txt').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in files))

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--windows',action='store_true'); parser.add_argument('--linux',action='store_true'); parser.add_argument('--iscc'); args=parser.parse_args()
    if args.linux:linux()
    if args.windows:windows(args.iscc)
    manifest()

if __name__=='__main__':main()
