"""Own a Windows worker and all descendants, including startup/cancel races."""
import ctypes
from ctypes import wintypes
import subprocess


class _IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
               ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _Basic(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit",ctypes.c_longlong),
        ("PerJobUserTimeLimit",ctypes.c_longlong),("LimitFlags",wintypes.DWORD),
        ("MinimumWorkingSetSize",ctypes.c_size_t),("MaximumWorkingSetSize",ctypes.c_size_t),
        ("ActiveProcessLimit",wintypes.DWORD),("Affinity",ctypes.c_size_t),
        ("PriorityClass",wintypes.DWORD),("SchedulingClass",wintypes.DWORD)]


class _Extended(ctypes.Structure):
    _fields_=[("BasicLimitInformation",_Basic),("IoInfo",_IO),
              ("ProcessMemoryLimit",ctypes.c_size_t),("JobMemoryLimit",ctypes.c_size_t),
              ("PeakProcessMemoryUsed",ctypes.c_size_t),("PeakJobMemoryUsed",ctypes.c_size_t)]


class ProcessJob:
    def __init__(self, command, stderr):
        self.kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes=[wintypes.LPVOID,wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype=wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.handle=self.kernel.CreateJobObjectW(None,None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits=_Extended()
        limits.BasicLimitInformation.LimitFlags=0x2000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=stderr,text=True,encoding="utf-8",creationflags=subprocess.CREATE_NO_WINDOW)
        # Worker waits for RUN. It cannot start a model before job ownership exists.
        if not self.kernel.AssignProcessToJobObject(self.handle,int(self.process._handle)):
            error=ctypes.get_last_error()
            self.process.kill()
            self.process.wait(10)
            self.close()
            raise ctypes.WinError(error)
        self.process.stdin.write("RUN\n")
        self.process.stdin.flush()
        self.process.stdin.close()

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle=None

    def cancel(self):
        self.close()
        if self.process.poll() is None:
            self.process.wait(10)
