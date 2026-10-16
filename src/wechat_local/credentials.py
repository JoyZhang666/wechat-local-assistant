"""Current-user Windows DPAPI protection; no plaintext key in settings."""
import base64
import ctypes
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_=[("size",wintypes.DWORD),("data",ctypes.POINTER(ctypes.c_ubyte))]


def _convert(data,protect):
    buffer=(ctypes.c_ubyte*len(data)).from_buffer_copy(data)
    incoming=Blob(len(data),buffer)
    outgoing=Blob()
    crypt=ctypes.WinDLL("crypt32",use_last_error=True)
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    function=crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes=[ctypes.POINTER(Blob),wintypes.LPVOID,ctypes.POINTER(Blob),
                       wintypes.LPVOID,wintypes.LPVOID,wintypes.DWORD,ctypes.POINTER(Blob)]
    function.restype=wintypes.BOOL
    kernel.LocalFree.argtypes=[wintypes.LPVOID]
    kernel.LocalFree.restype=wintypes.LPVOID
    if not function(ctypes.byref(incoming),None,None,None,None,1,ctypes.byref(outgoing)):
        raise ValueError("无法使用当前Windows账户保护或读取API Key。")
    try:
        return ctypes.string_at(outgoing.data,outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def protect_key(key):
    if not key.strip() or any(c in key for c in ("\n","\r","\0")):
        raise ValueError("API Key为空或包含非法换行。")
    return base64.b64encode(_convert(key.strip().encode(),True)).decode("ascii")


def reveal_key(value):
    try:
        return _convert(base64.b64decode(value,validate=True),False).decode("utf-8")
    except Exception:
        raise ValueError("API Key无法解密，请在本机重新填写。") from None
