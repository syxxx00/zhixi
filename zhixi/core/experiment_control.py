"""Opt-in, checkpoint-boundary control for registered independent workers."""

import json
import os
from pathlib import Path
import sqlite3
import time

PAUSE_MARKER = 'EXPERIMENT_PAUSED_AT_BATCH_BOUNDARY'


def read(directory):
    path = Path(directory) / 'experiment-control.sqlite3'
    if not path.exists():
        return {}
    with sqlite3.connect(path, timeout=10) as db:
        row = db.execute('SELECT state, updated FROM control WHERE id=1').fetchone()
    return {'state': row[0], 'updated': row[1]} if row else {}


def write(directory, state):
    with sqlite3.connect(Path(directory) / 'experiment-control.sqlite3', timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY, state TEXT, updated REAL)')
        db.execute('INSERT OR REPLACE INTO control VALUES (1,?,?)', (state, time.time()))


def before_batch(directory, *, wait=False):
    if read(directory).get('state') == 'pause_requested':
        # The preceding model call has returned through analyze_batch and its
        # checkpoint transaction has committed. Do not start another call.
        if not wait:
            raise RuntimeError(PAUSE_MARKER)
        write(directory, 'parked')
    while wait and read(directory).get('state') == 'parked':
        time.sleep(0.25)


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # Access denied is not proof of exit.
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def parent_chain():
    """Resolve Windows venv launcher ancestry, without shelling out per batch."""
    if os.name != 'nt':
        return {os.getppid()}
    import ctypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    native = ctypes.WinDLL('ntdll')
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    native.NtQueryInformationProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    native.NtQueryInformationProcess.restype = ctypes.c_long
    result = set()
    pid = os.getppid()
    for _ in range(4):
        if not pid or pid in result:
            break
        result.add(pid)
        handle = kernel.OpenProcess(0x0400, False, pid)
        if not handle:
            break
        try:
            basic = (ctypes.c_void_p * 6)()
            if native.NtQueryInformationProcess(handle, 0, basic, ctypes.sizeof(basic), None):
                break
            pid = basic[5]
        finally:
            kernel.CloseHandle(handle)
    return result


def legacy_child_boundary(root):
    """Adopt already-running workers at their NEXT child invocation only.

    No process suspension, injection or mid-generation termination. A dedicated
    marker travels through the old worker's error path; ops presents it as a
    deliberate pause only after that worker records its terminal observation.
    """
    candidates = []
    for path in (Path(root) / 'data/ops-experiments').glob('*.json'):
        try:
            directory = Path(json.loads(path.read_text(encoding='utf-8'))['directory'])
            if read(directory).get('state') != 'pause_requested':
                continue
            raw = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
            if time.time() - raw.get('heartbeat_unix', 0) < 30:
                candidates.append((directory, raw.get('process_id')))
        except (OSError, ValueError, KeyError):
            continue
    if candidates:
        parents = parent_chain()
        for directory, pid in candidates:
            if pid in parents:
                before_batch(directory)
