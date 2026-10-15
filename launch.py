"""Idempotent desktop launcher. Does not import app or load a model itself."""

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser
from zhixi.core.runtime_config import load_paths


ROOT = Path(__file__).resolve().parent
HOST = '127.0.0.1'
PORT = 8177
URL = f'http://{HOST}:{PORT}/'
STARTUP_TIMEOUT = 90


def is_our_service(root, port=PORT):
    """An occupied port/HTTP200 alone does not establish service identity."""
    try:
        opener = build_opener(ProxyHandler({}))  # Local health must not use a proxy.
        with opener.open(f'http://{HOST}:{port}/api/health', timeout=1) as response:
            value = json.loads(response.read(65536))
        return (
            isinstance(value, dict)
            and isinstance(value.get('capabilities'), dict)
            and value['capabilities'].get('input_material_contract') == 1
            and Path(value.get('history_path') or '').resolve() == load_paths(root).db_path.resolve()
        )
        # A missing model may make health.ok false; the settings UI still works.
    except (OSError, URLError, ValueError, TypeError):
        return False


def port_open(port=PORT):
    try:
        with socket.create_connection((HOST, port), timeout=0.3):
            return True
    except OSError:
        return False


@contextmanager
def startup_lock(root, timeout=STARTUP_TIMEOUT):
    """Serialize double-clicks without stale lock files or stopping any server."""
    if os.name != 'nt':
        yield
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    key = hashlib.sha256(str(root.resolve()).lower().encode('utf-8')).hexdigest()
    handle = kernel.CreateMutexW(None, False, 'Local\\WordProcessingStart_' + key)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        status = kernel.WaitForSingleObject(handle, int(timeout * 1000))
        acquired = status in (0, 0x80)  # Normal acquisition or abandoned owner.
        if not acquired:
            raise RuntimeError('另一个启动操作尚未结束，请稍后再试。')
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def start_server(root, log_path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('ab') as log:
        log.write((f'\n--- launcher {time.strftime("%Y-%m-%d %H:%M:%S")} ---\n').encode('utf-8'))
        log.flush()
        return subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'zhixi.web.app:app', '--host', HOST, '--port', str(PORT)],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
        )


def ensure_service(
    root=ROOT,
    *,
    probe=None,
    occupied=None,
    spawn=None,
    clock=time.monotonic,
    sleep=time.sleep,
    timeout=STARTUP_TIMEOUT,
):
    probe = probe or (lambda: is_our_service(root))
    occupied = occupied or port_open
    spawn = spawn or start_server
    if probe():
        return 'reused'
    log_path = load_paths(root).data_dir / 'logs/server-startup.log'
    process = None
    if not occupied():
        print('正在启动枝析，等待网页服务就绪……', flush=True)
        process = spawn(root, log_path)
    else:
        print('端口已被使用，正在确认是否为本项目；不会重复启动或结束现有进程。', flush=True)
    deadline = clock() + timeout
    while clock() < deadline:
        if probe():
            return 'started' if process is not None else 'reused'
        if process is not None and process.poll() is not None:
            # A different launcher may have finished at the same moment.
            if probe():
                return 'reused'
            raise RuntimeError(f'后台启动失败（退出码 {process.returncode}）。请查看日志：{log_path}')
        sleep(0.4)
    if process is None:
        raise RuntimeError(f'{PORT} 端口有服务，但未确认是当前项目。没有停止任何进程；请检查端口占用。')
    raise RuntimeError(
        f'后台尚未在 {timeout} 秒内就绪，未强制结束进程。请先查看日志再决定是否重试：{log_path}'
    )


def main():
    try:
        with startup_lock(ROOT):
            state = ensure_service()
        print(('服务已在运行。' if state == 'reused' else '服务已就绪。') + '正在打开 ' + URL, flush=True)
        if not webbrowser.open(URL, new=2):
            raise RuntimeError(f'未能自动打开浏览器，后台仍在运行。请手动打开 {URL}')
        return 0
    except Exception as exc:
        print(f'启动未完成：{exc}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
