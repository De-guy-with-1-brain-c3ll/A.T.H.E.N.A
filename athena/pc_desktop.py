"""Small, explicit desktop actions; no arbitrary command input."""
import asyncio
import os
from pathlib import Path
import subprocess


class PCDesktop:
    async def execute(self, body):
        if body != {'action': 'open_terminal'}:
            raise ValueError('Only open_terminal is supported; commands are not accepted.')
        if os.name != 'nt':
            raise ValueError('The desktop terminal action requires a Windows PC.')
        from athena.tools.command import windows_desktop
        desktop = windows_desktop()
        shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        env = {name: value for name, value in os.environ.items()
               if not any(marker in name.upper() for marker in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL'))}
        process = subprocess.Popen([str(shell), '-NoLogo', '-NoProfile', '-NoExit'],
            cwd=desktop, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        await asyncio.sleep(.75)
        if process.poll() is not None:
            raise ValueError('The terminal exited before startup could be confirmed.')
        return {'launched': True, 'pid': process.pid, 'cwd': str(desktop), 'shell': 'powershell'}
