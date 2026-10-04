"""Bounded PC browser smoke test; vision is opt-in and may incur charges."""
import argparse
import asyncio
import base64
import os
from pathlib import Path
from athena.config import load_local_environment
from athena.pc_bridge import browser_request
from athena.tools.pc_browser import PCBrowserTool


async def run(vision=False):
    load_local_environment()
    root = Path(__file__).resolve().parents[1]
    key_file = root/'orange_pi/.pc-transfer-key'
    if not os.environ.get('ATHENA_PC_TRANSFER_KEY') and key_file.exists():
        os.environ['ATHENA_PC_TRANSFER_KEY'] = key_file.read_text().strip()
        os.environ['ATHENA_PC_UPLOAD_URL'] = 'http://192.168.33.187:8781/upload'
    state = await browser_request({'action': 'status'})
    assert not state['keyboard_enabled'], 'Disable keyboard control in the console before this test.'
    opened = await browser_request({'action': 'open', 'url': 'https://example.com/'})
    print('Opened PC page:', opened['url'], opened['title'])
    image = await browser_request({'action': 'screenshot'})
    raw = base64.b64decode(image['image'], validate=True)
    assert raw.startswith(b'\xff\xd8') and len(raw) > 1000
    try:
        await browser_request({'action': 'type', 'text': 'MUST_NOT_TYPE'})
    except ValueError:
        print('Disabled keyboard correctly rejected typing.')
    else:
        raise AssertionError('Keyboard guard failed.')
    print('Screenshot verified:', len(raw), 'bytes; no continuous capture.')
    if os.name == 'nt':
        output = root/'outputs/pc-browser-check.jpg'; output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
    if vision:
        result = await PCBrowserTool().execute({'action': 'inspect', 'question': 'What heading and purpose are visible on this page?'})
        print('Vision:', result.spoken_text[:1000])
        assert result.success, result.spoken_text
        print('Vision tokens:', result.data.get('vision_tokens'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vision', action='store_true')
    asyncio.run(run(parser.parse_args().vision))
