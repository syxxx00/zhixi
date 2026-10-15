"""On-demand, whole-machine telemetry. No model imports or process attribution."""

import csv
import ctypes
import math
import os
import shutil
import subprocess
import time
from threading import Lock

_lock = Lock()
_cached = None
_cached_at = 0.0


def _windows_metrics():
    if os.name != 'nt':
        return None, None
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong)
            for name in (
                'total',
                'available',
                'page_total',
                'page_available',
                'virtual_total',
                'virtual_available',
                'extended',
            )
        ]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    ram = None
    if kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        ram = {
            'used_bytes': memory.total - memory.available,
            'total_bytes': memory.total,
            'available_bytes': memory.available,
            'commit_available_bytes': memory.page_available,
        }

    def times():
        values = [wintypes.FILETIME() for _ in range(3)]
        if not kernel.GetSystemTimes(*(ctypes.byref(v) for v in values)):
            return None
        return [(v.dwHighDateTime << 32) | v.dwLowDateTime for v in values]

    first = times()
    time.sleep(0.1)
    last = times()
    cpu = None
    if first is not None and last is not None:
        idle, kernel_time, user = [b - a for a, b in zip(first, last)]
        total = kernel_time + user  # Windows kernel time includes idle time.
        if total > 0 and idle >= 0:
            cpu = round(max(0, min(100, 100 * (total - idle) / total)), 1)
    return cpu, ram


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except ValueError:
        return None


def _gpu_metrics():
    executable = shutil.which('nvidia-smi')
    if not executable:
        return []
    result = subprocess.run(
        [
            executable,
            '--query-gpu=name,utilization.gpu,memory.used,memory.total',
            '--format=csv,noheader,nounits',
        ],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=2,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        check=True,
    )
    cards = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) != 4:
            continue
        name, utilization, used, total = [cell.strip() for cell in row]
        cards.append(
            {
                'name': name,
                'percent': _number(utilization),
                'used_mib': _number(used),
                'total_mib': _number(total),
            }
        )
    return cards


def device_status(*, fresh=False):
    global _cached, _cached_at
    # One bounded read per 15 seconds across browser tabs, only when requested.
    with _lock:
        if not fresh and _cached is not None and time.monotonic() - _cached_at < 15:
            return _cached
        cpu, memory, gpus = None, None, []
        try:
            cpu, memory = _windows_metrics()
        except (OSError, AttributeError):
            pass
        try:
            gpus = _gpu_metrics()
        except (OSError, subprocess.SubprocessError):
            pass
        _cached = {
            'scope': 'machine',
            'sampled_at': time.time(),
            'cpu_percent': cpu,
            'memory': memory,
            'gpus': gpus,
        }
        _cached_at = time.monotonic()
        return _cached
