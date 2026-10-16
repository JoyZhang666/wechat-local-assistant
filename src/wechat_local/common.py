"""Shared resource boundaries. This module does not access user data on import."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path


class BusyError(RuntimeError):
    pass


class OutputTooLong(RuntimeError):
    pass


class ContextTooLong(RuntimeError):
    pass


class NamedMutex:
    def __init__(self, name):
        self.name = name
        self.handle = None

    def __enter__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.handle = kernel.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        result = kernel.WaitForSingleObject(self.handle, 0)
        if result not in (0, 0x80):
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise BusyError("另一个本地任务正在使用此资源，请结束该任务后重试。")
        self.kernel = kernel
        return self

    def __exit__(self, *_):
        if self.handle:
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("任务文件必须位于本机数据目录内部。")
    return path
