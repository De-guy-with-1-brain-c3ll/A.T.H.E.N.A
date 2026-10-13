"""One real terminal launch via model and signed PC bridge, plus an opt-in agent check."""
import argparse
import asyncio
import os
from pathlib import Path
import tempfile
from uuid import uuid4
from athena.config import load_local_environment
from athena.llm.deepseek import DeepSeekLanguageModel
from athena.pc_bridge import pc_request
from athena.pc_transfer import lan_address
from athena.settings.store import RuntimeSettingsStore
from athena.tools.models import ToolResult
from athena.tools.pc_desktop import PCDesktopTool
from athena.tools.registry import ToolRegistry


async def main(agent=False):
    load_local_environment()
    root = Path(__file__).resolve().parents[1]
    os.environ['ATHENA_PC_TRANSFER_KEY'] = (root/'orange_pi/.pc-transfer-key').read_text().strip()
    os.environ['ATHENA_PC_UPLOAD_URL'] = f'http://{lan_address()}:8781/upload'
    receipts = []

    class BridgeTerminal(PCDesktopTool):
        async def execute(self, arguments):
            result = await pc_request('/desktop', arguments)
            receipts.append(result)
            return ToolResult(bool(result.get('launched')), 'The PC confirmed a PowerShell terminal launch in Desktop.', result)

    with tempfile.TemporaryDirectory() as directory:
        registry = ToolRegistry(); registry.register(BridgeTerminal())
        model = DeepSeekLanguageModel(os.environ['DEEPSEEK_API_KEY'],
            os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash'), registry,
            RuntimeSettingsStore(Path(directory)/'settings.json'))
        opened = model._open_stream
        count = 0

        async def bounded(request):
            nonlocal count
            count += 1
            if count > 4: raise RuntimeError('Live model check exceeded four requests.')
            return await opened(request)

        model._open_stream = bounded
        try:
            answer = ''.join([part async for part in model.stream_reply(uuid4(), 'Open a terminal on my desktop.')])
            if len(receipts) != 1 or not receipts[0].get('launched'):
                raise RuntimeError('Model did not receive exactly one verified PC launch receipt.')
            if os.name == 'nt':
                import psutil
                process = psutil.Process(receipts[0]['pid'])
                assert process.name().casefold() == 'powershell.exe'
                assert Path(process.cwd()).resolve() == Path(receipts[0]['cwd']).resolve()
            print('Model answer:', answer, flush=True)
            print('Verified terminal PID:', receipts[0]['pid'], '| Model requests:', count, flush=True)
        finally:
            await model.close()
            await registry.close()
    if agent:
        result = await pc_request('/openclaw', {'objective':
            'Verification only: run node --version on this Windows PC and report the exact output. Do not modify files or launch a terminal.'}, timeout=335)
        if not result['success']: raise RuntimeError(result['spoken_text'])
        ident = result['data']['task_id']
        print('PC OpenClaw submitted task:', ident, flush=True)
        for _ in range(170):
            result = await pc_request('/openclaw', {'action': 'report', 'id': ident})
            if not result['success']: raise RuntimeError(result['spoken_text'])
            row = result['data']['tasks'][0]
            if row['state'] == 'reply_received':
                print('PC OpenClaw reply:', row['report'], flush=True)
                break
            if row['state'] == 'unconfirmed': raise RuntimeError(row['report'])
            await asyncio.sleep(2)
        else:
            raise RuntimeError('No confirmed final reply. Inspect task ' + ident + ' before retrying.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.agent))
