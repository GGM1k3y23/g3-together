"""Where a hung game is stuck: every thread of a 32-bit process (Gothic3.exe under WOW64), its instruction pointer and the
return addresses found on its stack, as module+offset.

    hangstack.py <pid> [out.txt]

together.py calls sample() before it kills a game that does not close (22.10.: three hangs without a crash line and
without a GPU reset, at the start and during a load; the mod's log only said where it stopped writing). The stack is
scanned, not walked: a value counts as a return address when it points into a module and the bytes before it are a
call instruction, so a line can be a stale frame; the first lines of the oldest thread (the game's main thread) are the
ones to read. Each thread is suspended only while its context and stack are read."""
import ctypes
import sys
from ctypes import wintypes as W

PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x0400, 0x0010
THREAD_SUSPEND_RESUME, THREAD_GET_CONTEXT, THREAD_QUERY_INFORMATION = 0x0002, 0x0008, 0x0040
TH32CS_SNAPTHREAD = 0x4
LIST_MODULES_32BIT = 1
WOW64_CONTEXT_FULL = 0x10000 | 0x1 | 0x2  # i386 | control | integer
STACK_BYTES = 64 * 1024
FRAMES = 60


class FloatSave(ctypes.Structure):
    _fields_ = [('ControlWord', W.DWORD), ('StatusWord', W.DWORD), ('TagWord', W.DWORD), ('ErrorOffset', W.DWORD),
                ('ErrorSelector', W.DWORD), ('DataOffset', W.DWORD), ('DataSelector', W.DWORD),
                ('RegisterArea', ctypes.c_ubyte * 80), ('Cr0NpxState', W.DWORD)]


class Wow64Context(ctypes.Structure):
    _fields_ = [('ContextFlags', W.DWORD), ('Dr0', W.DWORD), ('Dr1', W.DWORD), ('Dr2', W.DWORD), ('Dr3', W.DWORD),
                ('Dr6', W.DWORD), ('Dr7', W.DWORD), ('FloatSave', FloatSave), ('SegGs', W.DWORD), ('SegFs', W.DWORD),
                ('SegEs', W.DWORD), ('SegDs', W.DWORD), ('Edi', W.DWORD), ('Esi', W.DWORD), ('Ebx', W.DWORD), ('Edx', W.DWORD),
                ('Ecx', W.DWORD), ('Eax', W.DWORD), ('Ebp', W.DWORD), ('Eip', W.DWORD), ('SegCs', W.DWORD), ('EFlags', W.DWORD),
                ('Esp', W.DWORD), ('SegSs', W.DWORD), ('ExtendedRegisters', ctypes.c_ubyte * 512)]


class ThreadEntry(ctypes.Structure):
    _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ThreadID', W.DWORD), ('th32OwnerProcessID', W.DWORD),
                ('tpBasePri', W.LONG), ('tpDeltaPri', W.LONG), ('dwFlags', W.DWORD)]


class ModuleInfo(ctypes.Structure):
    _fields_ = [('lpBaseOfDll', ctypes.c_void_p), ('SizeOfImage', W.DWORD), ('EntryPoint', ctypes.c_void_p)]


kernel = ctypes.WinDLL('kernel32', use_last_error=True)
psapi = ctypes.WinDLL('psapi', use_last_error=True)
kernel.OpenProcess.restype = W.HANDLE
kernel.OpenThread.restype = W.HANDLE
kernel.CreateToolhelp32Snapshot.restype = W.HANDLE
kernel.ReadProcessMemory.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
psapi.EnumProcessModulesEx.argtypes = [W.HANDLE, ctypes.POINTER(W.HMODULE), W.DWORD, ctypes.POINTER(W.DWORD), W.DWORD]
psapi.GetModuleBaseNameW.argtypes = [W.HANDLE, W.HMODULE, W.LPWSTR, W.DWORD]
psapi.GetModuleInformation.argtypes = [W.HANDLE, W.HMODULE, ctypes.POINTER(ModuleInfo), W.DWORD]


def modules(process):
    handles = (W.HMODULE * 1024)()
    needed = W.DWORD()
    if not psapi.EnumProcessModulesEx(process, handles, ctypes.sizeof(handles), ctypes.byref(needed), LIST_MODULES_32BIT):
        return []
    found = []
    for module in handles[:needed.value // ctypes.sizeof(W.HMODULE)]:
        name = ctypes.create_unicode_buffer(260)
        info = ModuleInfo()
        if psapi.GetModuleBaseNameW(process, module, name, 260) and \
                psapi.GetModuleInformation(process, module, ctypes.byref(info), ctypes.sizeof(info)):
            found.append((info.lpBaseOfDll or 0, info.SizeOfImage, name.value))
    return sorted(found)


def where(address, mods):
    for base, size, name in mods:
        if base <= address < base + size:
            return f'{name}+{address - base:#x}'
    return None


def read(process, address, size):
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not kernel.ReadProcessMemory(process, ctypes.c_void_p(address), buffer, size, ctypes.byref(got)):
        return b''
    return buffer.raw[:got.value]


def read_stack(process, esp):
    """From esp up to STACK_BYTES, page by page: a read past the top of the stack fails as a whole."""
    data = read(process, esp, 0x1000 - (esp & 0xFFF))
    while data and len(data) < STACK_BYTES:
        page = read(process, esp + len(data), 0x1000)
        if not page:
            break
        data += page
    return data


def after_call(process, address):
    """The bytes before `address` end in a call: E8 rel32, FF 15 [abs], FF /2 with a register or a displacement."""
    b = read(process, address - 7, 7)
    if len(b) < 7:
        return False
    return (b[2] == 0xE8 or (b[1] == 0xFF and b[2] in (0x15, 0x90, 0x91, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97))
            or (b[5] == 0xFF and (b[6] & 0xF8) == 0xD0) or (b[4] == 0xFF and (b[5] & 0xF8) in (0x50, 0x10) and (b[5] & 0x38) == 0x10)
            or (b[5] == 0xFF and (b[6] & 0x38) == 0x10 and b[6] < 0xC0 and (b[6] & 7) not in (4, 5)))


def thread_ids(pid):
    snap = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    entry = ThreadEntry()
    entry.dwSize = ctypes.sizeof(entry)
    ids = []
    ok = kernel.Thread32First(snap, ctypes.byref(entry))
    while ok:
        if entry.th32OwnerProcessID == pid:
            ids.append(entry.th32ThreadID)
        ok = kernel.Thread32Next(snap, ctypes.byref(entry))
    kernel.CloseHandle(snap)
    return ids


def created(thread):
    times = [W.FILETIME() for _ in range(4)]
    kernel.GetThreadTimes(thread, *[ctypes.byref(t) for t in times])
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


def sample(pid):
    """Text: one block per thread, the oldest (the game's main thread) first."""
    process = kernel.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not process:
        return f'hangstack: cannot open process {pid} (error {ctypes.get_last_error()})\n'
    mods = modules(process)
    blocks = []
    for tid in thread_ids(pid):
        thread = kernel.OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, False, tid)
        if not thread:
            continue
        context = Wow64Context()
        context.ContextFlags = WOW64_CONTEXT_FULL
        stack = b''
        if kernel.Wow64SuspendThread(thread) != 0xFFFFFFFF:
            try:
                if kernel.Wow64GetThreadContext(thread, ctypes.byref(context)):
                    stack = read_stack(process, context.Esp)
            finally:
                kernel.ResumeThread(thread)
        born = created(thread)
        kernel.CloseHandle(thread)
        frames = []
        for at in range(0, len(stack) - 3, 4):
            value = int.from_bytes(stack[at:at + 4], 'little')
            name = where(value, mods)
            if name and after_call(process, value):
                frames.append(f'  esp+{at:#06x} {name}')
                if len(frames) == FRAMES:
                    break
        head = f'thread {tid} eip={where(context.Eip, mods) or hex(context.Eip)} esp={context.Esp:#x} ebp={context.Ebp:#x}'
        blocks.append((born, head + '\n' + '\n'.join(frames)))
    kernel.CloseHandle(process)
    return f'hangstack: process {pid}, {len(blocks)} threads, {len(mods)} modules; oldest thread first\n' + \
        '\n'.join(text for _, text in sorted(blocks)) + '\n'


if __name__ == '__main__':
    text = sample(int(sys.argv[1]))
    if len(sys.argv) > 2:
        open(sys.argv[2], 'w', encoding='utf-8').write(text)
    print(text)
