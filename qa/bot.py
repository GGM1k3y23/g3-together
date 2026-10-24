"""Shared helpers for the bot-driven suites: drive one game process through the mod's QA command file."""
import ctypes
import hashlib
import json
import os
import sys
import time

from workspace import WORKSPACE

GAME = WORKSPACE/'test-install'
RUNS = WORKSPACE/'runtime/runs'
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'


def snapshot(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file()} if path.is_dir() else {}


def emit(value):
    print(json.dumps(value), flush=True)


class Bot:
    """Sends sequenced commands to the mod's QA bot (qa-command.txt) and waits for its log lines (qa-engine.log)."""

    def __init__(self, game, run, deadline, root=GAME, name='QAHost'):
        self.game, self.run, self.deadline, self.sequence = game, run, deadline, 1
        self.root, self.name = root, name

    def log(self):
        path = self.root/'qa-engine.log'
        return path.read_text(errors='replace') if path.exists() else ''

    def wait(self, predicate, seconds, what, start=0):
        until = min(self.deadline, time.monotonic()+seconds)
        while time.monotonic() < until:
            if self.game.poll() is not None:
                raise RuntimeError(f'Game exited with {self.game.returncode} while waiting for {what}')
            if (self.run/'stop-requested').exists():
                raise RuntimeError('Stop requested')
            found = predicate(self.log()[start:])
            if found:
                return found
            time.sleep(0.5)
        raise RuntimeError('Timed out waiting for '+what)

    def send(self, action, seconds=10):
        """Queue one command and wait for its acknowledgement; return the log offset before it."""
        self.sequence += 1
        start = len(self.log())
        # The mod reads this file every frame; a write can collide with that read (sharing violation).
        for attempt in range(20):
            try:
                (self.root/'qa-command.txt').write_text(f'{self.sequence} {action}\n')
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
        tag = f'COMMAND sequence={self.sequence} action={action} '
        self.wait(lambda text: tag in text, seconds, 'acknowledgement of '+action, start)
        return start

    def line(self, prefix, seconds, what, start):
        return self.wait(lambda text: next((l for l in text.splitlines() if l.startswith(prefix)), ''), seconds, what, start)


def post_key(pid, key):
    """Queue key down/up to the visible top-level windows of our own game process. Returns windows hit."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    found = []
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        owner = ctypes.c_ulong()
        user.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(ctypes.c_void_p(hwnd)):
            found.append(hwnd)
        return True
    user.EnumWindows(visit, None)
    from ctypes import wintypes
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    scan = user.MapVirtualKeyW(key, 0)
    for hwnd in found:
        user.PostMessageW(ctypes.c_void_p(hwnd), 0x0100, key, 1 | scan << 16)
        user.PostMessageW(ctypes.c_void_p(hwnd), 0x0101, key, 1 | scan << 16 | 0xC0000000)
    return len(found)


def post_rclick(pid):
    """Queue a right mouse click (down/up, window centre) to the visible top-level windows of our own game process."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    from ctypes import wintypes
    found = []
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        owner = ctypes.c_ulong()
        user.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(ctypes.c_void_p(hwnd)):
            found.append(hwnd)
        return True
    user.EnumWindows(visit, None)
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    for hwnd in found:
        rect = wintypes.RECT()
        user.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
        at = (rect.right // 2) | (rect.bottom // 2) << 16
        user.PostMessageW(ctypes.c_void_p(hwnd), 0x0204, 0x0002, at)  # WM_RBUTTONDOWN, MK_RBUTTON
        user.PostMessageW(ctypes.c_void_p(hwnd), 0x0205, 0, at)       # WM_RBUTTONUP
    return len(found)


def post_click(pid, fx, fy):
    """Queue a left mouse click at (fx, fy), fractions of the client area, to the visible top-level windows of our own
    game process (the trade screen's tip box: its Ok takes no key, C trade-vendor-empty-followup-c.md)."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    from ctypes import wintypes
    found = []
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        owner = ctypes.c_ulong()
        user.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(ctypes.c_void_p(hwnd)):
            found.append(hwnd)
        return True
    user.EnumWindows(visit, None)
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    for hwnd in found:
        rect = wintypes.RECT()
        user.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
        at = int(rect.right * fx) | int(rect.bottom * fy) << 16
        user.PostMessageW(ctypes.c_void_p(hwnd), 0x0200, 0, at)       # WM_MOUSEMOVE
        user.PostMessageW(ctypes.c_void_p(hwnd), 0x0201, 0x0001, at)  # WM_LBUTTONDOWN, MK_LBUTTON
        user.PostMessageW(ctypes.c_void_p(hwnd), 0x0202, 0, at)       # WM_LBUTTONUP
    return len(found)


def place_on_second_screen(pid, slot, seconds=90):
    """Place the game window of `pid` side by side on a monitor: the primary one, or with G3_SCREEN=2 a secondary one.
    slot 0 = left half, 1 = right half of that monitor. Runs in a thread: retries while the game creates and
    resizes its window. Does nothing if no such monitor exists."""
    import threading
    from ctypes import wintypes

    # G3_SCREEN=2 puts the windows on a secondary monitor; default (1) is the primary one (owner's wish, 2026-10-18).
    want_primary = os.environ.get('G3_SCREEN', '1') != '2'

    def run():
        user = ctypes.WinDLL('user32', use_last_error=True)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]
        screens = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        def monitor(handle, dc, rect, data):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            user.GetMonitorInfoW(handle, ctypes.byref(info))
            primary = bool(info.dwFlags & 1)
            if primary == want_primary:
                screens.append((info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom))
            return True
        user.EnumDisplayMonitors(None, None, monitor, 0)
        if not screens:
            return
        left, top, right, _ = screens[0]
        x = left + 10 + slot * ((right - left) // 2)
        until = time.monotonic() + seconds
        placed = 0
        while time.monotonic() < until and placed < 3:
            found = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def visit(hwnd, _):
                owner = ctypes.c_ulong()
                user.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
                if owner.value == pid and user.IsWindowVisible(ctypes.c_void_p(hwnd)):
                    found.append(hwnd)
                return True
            user.EnumWindows(visit, None)
            for hwnd in found:
                rect = wintypes.RECT()
                user.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
                if rect.left != x or rect.top != top + 10:
                    user.SetWindowPos(ctypes.c_void_p(hwnd), None, x, top + 10, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE
                else:
                    placed += 1
            time.sleep(2)
    threading.Thread(target=run, daemon=True).start()
