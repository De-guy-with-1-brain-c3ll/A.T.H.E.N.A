"""Live Pi checks: real downloads, sandboxed code, PC receipts, Teams and silent stop."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from athena.config import Settings, load_local_environment
from athena.paths import data_directory
from athena.settings.store import RuntimeSettingsStore


async def transfers():
    from athena.tools.coding import CodingWorkspaceTool
    from athena.tools.download import DownloadTool
    from athena.tools.pc_transfer import UploadTool
    nonce = str(int(time.time()))
    coding = CodingWorkspaceTool()
    receipts = []
    async def send(path, expected):
        upload = UploadTool()
        prepared, _ = await upload.prepare({'path': str(path)})
        result = await upload.execute(prepared)
        assert result.success, result.spoken_text
        assert result.data['bytes'] == len(expected)
        assert result.data['sha256'] == hashlib.sha256(expected).hexdigest()
        assert result.data['percent_complete'] == 100
        receipts.append(result.data)
        print('PASS transfer', Path(path).name, len(expected), result.data['sha256'], flush=True)
    for index, source in enumerate(('print("Hello world")\n',
            'def add(a, b):\n    return a + b\n\nprint(add(17, 25))\n',
            '#' + 'large coding transfer ' * 2300 + '\nprint(42)\n')):
        project = f'release_qa_{nonce}_{index}'
        assert (await coding.execute({'action': 'create', 'project': project})).success
        result = await coding.execute({'action': 'write', 'project': project,
                                      'path': 'main.py', 'content': source})
        assert result.success, result.spoken_text
        checked = await coding.execute({'action': 'check', 'project': project})
        assert checked.success, checked.spoken_text
        ran = await coding.execute({'action': 'run', 'project': project})
        assert ran.success, (ran.spoken_text, ran.data)
        expected = 'Hello world' if index == 0 else '42'
        assert expected in ran.data.get('stdout', ''), ran.data
        await send(result.data['path'], source.encode())
    project = f'release_qa_{nonce}_1'
    test = 'import unittest\nfrom main import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(17, 25), 42)\n'
    await coding.execute({'action': 'write', 'project': project, 'path': 'test_main.py', 'content': test})
    tested = await coding.execute({'action': 'test', 'project': project})
    assert tested.success, (tested.spoken_text, tested.data)
    print('PASS coding unittest', flush=True)
    for index, relative in enumerate(('README.rst', 'LICENSE', 'Lib/test/test_asyncio/test_tasks.py')):
        download = DownloadTool()
        result = await download.execute({'url': 'https://raw.githubusercontent.com/python/cpython/main/' + relative,
            'filename': f'release-qa-{nonce}-{index}.txt'})
        assert result.success, result.spoken_text
        payload = Path(result.data['path']).read_bytes()
        assert payload, 'empty public download'
        print('PASS public download', relative, len(payload), flush=True)
        await send(result.data['path'], payload)
    (data_directory() / 'release-qa-receipts.json').write_text(json.dumps(receipts, indent=2))


async def teams():
    from athena.tools.teams import TeamsAssignmentsTool
    tool = TeamsAssignmentsTool()
    try:
        for arguments in ({'limit': 10}, {'limit': 10, 'class_name': 'physics'}):
            result = await tool.execute(arguments)
            assert result.success, result.spoken_text
            assert result.data['assignments'], 'Expected assignments from live enrolled classes'
            print('PASS Teams', arguments, len(result.data['assignments']),
                  'instructions', sum(bool(row.get('instructions')) for row in result.data['assignments']), flush=True)
    finally:
        await tool.close()


async def interruptions():
    import edge_tts
    from athena.coordinator import VoiceCoordinator
    from athena.stt import build_recognizer
    settings_store = RuntimeSettingsStore()
    settings = Settings.from_environment(settings_store)
    root = data_directory() / 'silent-interruption-qa'
    root.mkdir(exist_ok=True)
    for index, phrase in enumerate(('Athena stop talking', 'Stop', 'Be quiet')):
        mp3 = root / f'{index}.mp3'
        await edge_tts.Communicate(phrase, 'en-US-AvaNeural').save(str(mp3))
        pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(mp3), '-f', 's16le',
            '-ar', '16000', '-ac', '1', 'pipe:1'])
        class Mic:
            async def frames(self):
                for offset in range(0, len(pcm), 640):
                    yield pcm[offset:offset + 640].ljust(640, b'\0')
                    await asyncio.sleep(.02)
                while True:
                    yield bytes(640)
                    await asyncio.sleep(.02)
        recognizer = build_recognizer(settings)
        await recognizer.connect()
        coordinator = VoiceCoordinator.__new__(VoiceCoordinator)
        coordinator.microphone = Mic()
        coordinator.interruption_stt = recognizer
        coordinator.settings_store = settings_store
        coordinator._stop_listening = AsyncMock()
        coordinator.cancel_active_turn = AsyncMock()
        cancelled = asyncio.Event()
        async def silent_output():
            try: await asyncio.sleep(20)
            finally: cancelled.set()
        started = time.monotonic()
        try:
            await asyncio.wait_for(coordinator._interruptible_speech(silent_output()), 15)
            assert cancelled.is_set()
            coordinator.cancel_active_turn.assert_awaited_once()
            print('PASS real STT silent interruption', phrase, round(time.monotonic() - started, 2), 'seconds', flush=True)
        finally:
            await recognizer.close()


async def main():
    load_local_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('case', choices=('transfers', 'teams', 'interruptions'))
    arguments = parser.parse_args()
    await globals()[arguments.case]()


if __name__ == '__main__':
    asyncio.run(main())
