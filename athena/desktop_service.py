"""On-demand Windows UI automation. No global observer or arbitrary shell input."""
import asyncio
import base64
import io
import os


async def execute(body):
    if os.name != 'nt':
        raise ValueError('Desktop automation requires Windows.')
    if body['action'] == 'open_terminal':
        from athena.pc_desktop import PCDesktop
        return await PCDesktop().execute({'action': 'open_terminal'})

    def perform():
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        action = body['action']
        if action == 'windows':
            windows = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def capture(handle, _):
                if user32.IsWindowVisible(handle):
                    length = user32.GetWindowTextLengthW(handle)
                    if length:
                        text = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(handle, text, length + 1)
                        windows.append({'handle': int(handle), 'title': text.value[:200]})
                return True
            user32.EnumWindows(callback_type(capture), 0)
            return {'windows': windows}
        if action == 'screenshot':
            from PIL import ImageGrab
            rectangle = wintypes.RECT()
            window = body.get('window')
            if window and not user32.GetWindowRect(window, ctypes.byref(rectangle)):
                raise ValueError('Window no longer exists.')
            box = (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom) if window else None
            image = ImageGrab.grab(bbox=box)
            stream = io.BytesIO(); image.save(stream, format='PNG')
            return {'image': base64.b64encode(stream.getvalue()).decode(), 'mime': 'image/png',
                    'width': image.width, 'height': image.height,
                    'window': window, 'captured_on_request': True}
        if not body.get('window'):
            raise ValueError('Choose an explicit window handle before interacting.')
        handle = wintypes.HWND(body['window'])
        if not user32.IsWindow(handle): raise ValueError('Window no longer exists.')
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow(handle)
        if user32.GetForegroundWindow() != body['window']:
            # Cross-thread foreground activation can be queued. Temporarily
            # join the foreground input queue, then detach before sending keys.
            current = ctypes.windll.kernel32.GetCurrentThreadId()
            foreground = user32.GetForegroundWindow()
            foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
            attached = user32.AttachThreadInput(current, foreground_thread, True)
            try:
                user32.ShowWindow(handle, 9)
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)
            finally:
                if attached: user32.AttachThreadInput(current, foreground_thread, False)
            import time
            for _ in range(20):
                if user32.GetForegroundWindow() == body['window']: break
                time.sleep(.01)
        if user32.GetForegroundWindow() != body['window']:
            raise ValueError('Windows did not focus the selected window; no input was sent.')
        rectangle = wintypes.RECT()
        user32.GetWindowRect(handle, ctypes.byref(rectangle))
        if action == 'click':
            x, y = body['x'], body['y']
            if not (0 <= x < rectangle.right - rectangle.left and 0 <= y < rectangle.bottom - rectangle.top):
                raise ValueError('Click must be inside the selected window.')
            user32.SetCursorPos(rectangle.left + x, rectangle.top + y)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
        elif action == 'type':
            text = body['text']
            if len(text) > 4000: raise ValueError('Typing is limited to 4,000 characters.')
            if any(ord(c) < 32 for c in text): raise ValueError('Use explicit key actions for control characters.')
            pointer = ctypes.c_size_t
            class Keyboard(ctypes.Structure):
                _fields_ = [('vk', wintypes.WORD), ('scan', wintypes.WORD), ('flags', wintypes.DWORD),
                            ('time', wintypes.DWORD), ('extra', pointer)]
            class Mouse(ctypes.Structure):
                _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG), ('data', wintypes.DWORD),
                            ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('extra', pointer)]
            class Union(ctypes.Union): _fields_ = [('keyboard', Keyboard), ('mouse', Mouse)]
            class Input(ctypes.Structure): _fields_ = [('type', wintypes.DWORD), ('value', Union)]
            encoded = text.encode('utf-16-le')
            packets = []
            for index in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[index:index + 2], 'little')
                for flags in (4, 6): packets.append(Input(1, Union(keyboard=Keyboard(0, unit, flags, 0, 0))))
            array = (Input * len(packets))(*packets)
            if packets and user32.SendInput(len(packets), array, ctypes.sizeof(Input)) != len(packets):
                raise ValueError('Windows blocked keyboard input; target changes are unconfirmed.')
        elif action == 'press':
            keys = {'enter': (13,), 'tab': (9,), 'escape': (27,), 'backspace': (8,),
                    'ctrl+a': (17, 65), 'ctrl+c': (17, 67), 'ctrl+v': (17, 86)}
            sequence = keys.get(body['key'].lower())
            if not sequence: raise ValueError('Unsupported key.')
            for key in sequence: user32.keybd_event(key, 0, 0, 0)
            for key in reversed(sequence): user32.keybd_event(key, 0, 2, 0)
        elif action != 'focus':
            raise ValueError('Unsupported desktop action.')
        return {'action': action, 'window': body['window'], 'input_sent': True,
                'target_effect_requires_verification': True}
    return await asyncio.to_thread(perform)
