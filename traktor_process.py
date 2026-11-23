"""Is Traktor running? Reads Windows' process list with the standard library only (no extra package).

RIPPER_TRAKTOR_PROCESS changes the name prefix it looks for (default "traktor pro", which matches "Traktor Pro 4.exe" and
"Traktor Pro 3.exe" but not Traktor's audio drivers or controller services); the tests use a stand-in program.
"""
import ctypes
import os
import sys

DEFAULT_PREFIX = "traktor pro"


def is_traktor_name(name, prefix=None):
    prefix = (prefix or os.environ.get("RIPPER_TRAKTOR_PROCESS") or DEFAULT_PREFIX).lower()
    lowered = (name or "").lower()
    return lowered.startswith(prefix) and lowered.endswith(".exe")


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong), ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260)]


def process_list():
    """[(pid, exe name)] of every running process; empty when it cannot be read or this is not Windows."""
    if sys.platform != "win32":
        return []
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    found = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            found.append((entry.th32ProcessID, entry.szExeFile))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def traktor_pids():
    """The process ids of running Traktor Pro programs."""
    return {pid for pid, name in process_list() if is_traktor_name(name)}
