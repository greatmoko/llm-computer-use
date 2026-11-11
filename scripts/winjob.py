"""Windows Job Object：把 PI 子进程树纳入可整体终止的作业对象。

- 作业设置 KILL_ON_JOB_CLOSE：Python 宿主（无论正常退出还是崩溃）句柄关闭时，
  整棵子进程树（cmd/node/bash 等）被内核直接终止，不依赖应用层清理。
- 子进程以 CREATE_SUSPENDED 启动，指派作业后再恢复主线程，避免子进程在
  指派前抢先创建逃逸的孙进程。
- 仅支持 Windows；非 Windows 平台退化为直接 Popen（当前产品仅面向 Windows）。
"""
import ctypes
import subprocess
import sys
from ctypes import wintypes

if sys.platform == "win32":
    kernel32 = ctypes.windll.kernel32

    # 64 位下句柄是 64 位指针；不声明 restype/argtypes 会被截断为 32 位 int。
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_TERMINATE = 0x8
    CREATE_SUSPENDED = 0x4
    PROCESS_SET_QUOTA = 0x100
    PROCESS_TERMINATE = 0x1
    THREAD_SUSPEND_RESUME = 0x2
    TH32CS_SNAPTHREAD = 0x4
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class THREADENTRY32(ctypes.Structure):
        # Win11 x64 上 Thread32First 要求 dwSize=28（24 会报 ERROR_BAD_LENGTH），
        # 补一个尾部保留字段使 sizeof 对齐到 API 期望的大小。
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpDeltaPri", ctypes.c_long),
                    ("tpPri", ctypes.c_long),
                    ("reserved", wintypes.DWORD)]


class JobAssignmentError(Exception):
    pass


class ProcessJob:
    """持有一个作业对象及其唯一子进程。"""

    def __init__(self, argv, cwd, env, **popen_kwargs):
        if sys.platform != "win32":
            self.proc = subprocess.Popen(argv, cwd=cwd, env=env, **popen_kwargs)
            self.handle = None
            return
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise JobAssignmentError("无法创建作业对象")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(self.handle)
            raise JobAssignmentError("无法配置作业对象")
        try:
            flags = popen_kwargs.pop("creationflags", 0) | CREATE_SUSPENDED
            self.proc = subprocess.Popen(argv, cwd=cwd, env=env, creationflags=flags, **popen_kwargs)
        except OSError:
            kernel32.CloseHandle(self.handle)
            raise
        process_handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, self.proc.pid)
        if not process_handle:
            self._resume_threads()
            self.proc.kill()
            self.proc.wait(5)
            kernel32.CloseHandle(self.handle)
            raise JobAssignmentError("无法打开子进程句柄")
        assigned = kernel32.AssignProcessToJobObject(self.handle, process_handle)
        kernel32.CloseHandle(process_handle)
        resumed = self._resume_threads()
        if not assigned or not resumed:
            self.terminate(0)
            raise JobAssignmentError("子进程未能纳入作业对象")

    def _resume_threads(self):
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return False
        resumed = False
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == self.proc.pid:
                thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    kernel32.ResumeThread(thread)
                    kernel32.CloseHandle(thread)
                    resumed = True
            found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        kernel32.CloseHandle(snapshot)
        return resumed

    def terminate(self, timeout=5):
        """终止整棵进程树并等待退出；返回是否确认退出。"""
        if self.proc.poll() is not None:
            return True
        if sys.platform == "win32" and self.handle:
            kernel32.TerminateJobObject(self.handle, 1)
        else:
            self.proc.kill()
        try:
            self.proc.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def close(self):
        """关闭作业句柄（触发 KILL_ON_JOB_CLOSE 兜底）并释放进程对象。"""
        if sys.platform == "win32" and self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
