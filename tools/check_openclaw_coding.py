"""Live ATHENA coding delegation, background progress and independently checked artifact."""
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.pc_transfer import lan_address
from athena.openclaw import installation
from athena.settings.store import RuntimeSettingsStore
from athena.tools.openclaw import OpenClawTaskTool, OpenClawStatusTool
from athena.tools.registry import ToolRegistry


async def main():
    load_local_environment()
    root = Path(__file__).resolve().parents[1]
    os.environ['ATHENA_PC_TRANSFER_KEY'] = (root/'orange_pi/.pc-transfer-key').read_text().strip()
    os.environ['ATHENA_PC_UPLOAD_URL'] = f'http://{lan_address()}:8781/upload'
    artifact = installation() / 'workspace' / ('coding-tool-check-' + uuid4().hex[:8] + '.py')
    objective = (f'Write a standalone Python program at {artifact.as_posix()} that defines add(a,b), '
        'asserts add(2,3)==5 and prints exactly OPENCLAW_CODING_OK when run. '
        'Run it using the provided Python executable and report the actual file path, '
        'test output and exit code. Do not change ATHENA source or other files.')
    with tempfile.TemporaryDirectory() as directory:
        registry = ToolRegistry()
        registry.register(OpenClawTaskTool(remote=True)); registry.register(OpenClawStatusTool(remote=True))
        model = DeepSeekLanguageModel(os.environ['DEEPSEEK_API_KEY'],
            os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash'), registry,
            RuntimeSettingsStore(Path(directory)/'settings.json'))
        opened = model._open_stream
        count = 0

        async def bounded(request):
            nonlocal count
            count += 1
            for message in request['messages'][-2:]:
                if message.get('role') == 'tool':
                    import json
                    receipt = json.loads(message['content'])
                    if receipt.get('error'): print('Model argument error:', receipt['error'], flush=True)
            if count > 6: raise RuntimeError('Live coding check exceeded six model requests.')
            return await opened(request)

        model._open_stream = bounded
        execute = registry.execute

        async def tracked(name, arguments, **kwargs):
            if name != 'openclaw_status':
                print('ATHENA tool call:', name, '| Argument keys:', sorted(arguments), flush=True)
            result = await execute(name, arguments, **kwargs)
            if name != 'openclaw_status':
                print('Tool receipt:', result.success, '| Pending approval:', bool(result.data.get('pending_approval')), flush=True)
            return result

        registry.execute = tracked
        try:
            reply = ''.join([part async for part in model.stream_reply(uuid4(), objective)])
            if not registry.has_pending_approval:
                raise RuntimeError('The model did not prepare the actual OpenClaw task.')
            print('ATHENA selected OpenClaw; actual approval prepared.', flush=True)
            registry.present_approval()
            result = await registry.handle_permission_reply('yes')  # This explicit verification task is authorized.
            if not result or not result.data.get('background_started'):
                raise RuntimeError('OpenClaw task was not confirmed submitted: ' + (result.spoken_text if result else reply))
            ident = result.data['task_id']; print(result.spoken_text, flush=True)
            deadline = time.monotonic() + 340
            previous = None
            while time.monotonic() < deadline:
                result = await registry.execute('openclaw_status', {'action': 'report', 'id': ident})
                if not result.success: raise RuntimeError(result.spoken_text)
                row = result.data['tasks'][0]
                stamp = (row['state'], row['progress'])
                if stamp != previous:
                    print(row['state'] + ': ' + row['progress'], flush=True); previous = stamp
                if row['state'] == 'reply_received':
                    print('Agent report:', row['report'], flush=True)
                    break
                if row['state'] == 'unconfirmed': raise RuntimeError(row['report'])
                await asyncio.sleep(2)
            else: raise RuntimeError('No final reply before the check deadline; do not resubmit automatically.')
            if not artifact.is_file(): raise RuntimeError('Reported artifact does not exist.')
            checked = subprocess.run([str(root/'.venv/Scripts/python.exe'), str(artifact)],
                capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            assert checked.returncode == 0 and checked.stdout.strip() == 'OPENCLAW_CODING_OK'
            status_reply = ''.join([part async for part in model.stream_reply(uuid4(),
                f'Give me the final report on OpenClaw task {ident}.')])
            assert ident in status_reply
            print('ATHENA final report retrieved without restarting work.', flush=True)
            print('Independently verified saved file, test output and exit code. Model requests:', count, flush=True)
        finally:
            await model.close(); await registry.close()


if __name__ == '__main__': asyncio.run(main())
