"""只读核对进程PID及出生身份；不终止进程，不按实例开启推断死亡。"""
import os
import re
import sys
from pathlib import Path
from uuid import UUID, uuid4


def _darwin_process_identity(pid):
    """按本机SDK的PROC_PIDTBSDINFO读取微秒出生时间；只读，不发送进程信号。

    结构对应sys/proc_info.h的proc_bsdinfo，使用秒+微秒整数避免PID复用和浮点截断。
    拒绝访问、短返回或身份不符均保持UNKNOWN，不能靠新实例启动推断旧实例已死。
    """
    import ctypes
    import errno

    if type(pid) is not int or not 1 <= pid <= 0x7FFFFFFF:
        return "UNKNOWN", None
    u32, u64 = ctypes.c_uint32, ctypes.c_uint64

    class BSDInfo(ctypes.Structure):
        _fields_ = [(name, u32) for name in (
            "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid",
            "svuid", "svgid", "reserved")] + [
            ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ] + [(name, u32) for name in ("nfiles", "pgid", "pjobc", "tdev", "tpgid")] + [
            ("nice", ctypes.c_int32), ("start_sec", u64), ("start_usec", u64)]

    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        probe = library.proc_pidinfo
        probe.argtypes = [ctypes.c_int, ctypes.c_int, u64, ctypes.c_void_p, ctypes.c_int]
        probe.restype = ctypes.c_int
        info = BSDInfo()
        ctypes.set_errno(0)
        size = probe(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))  # PROC_PIDTBSDINFO
        if size == 0 and ctypes.get_errno() == errno.ESRCH:
            return "DEAD", None
        if size != ctypes.sizeof(info) or info.pid != pid:
            return "UNKNOWN", None
        if info.status == 5:  # SZOMB，内核已确认退出、尚未由父进程收集。
            return "DEAD", None
        if info.start_sec <= 0 or not 0 <= info.start_usec < 1_000_000:
            return "UNKNOWN", None
        return "ALIVE", str(info.start_sec * 1_000_000 + info.start_usec)
    except (OSError, AttributeError, OverflowError):
        return "UNKNOWN", None


def process_identity(pid):
    """返回ALIVE+出生标识、已证明DEAD或无法证明的UNKNOWN。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FileTime)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ("DEAD" if ctypes.get_last_error() in {87, 1168} else "UNKNOWN"), None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return "UNKNOWN", None
            if code.value != 259:
                return "DEAD", None
            times = [FileTime() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                return "UNKNOWN", None
            created = times[0]
            return "ALIVE", str((created.high << 32) | created.low)
        finally:
            kernel.CloseHandle(handle)
    if sys.platform == "darwin":
        return _darwin_process_identity(pid)
    try:
        # Linux starttime是内核记录；先剥离可能含空格的comm字段。
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return "DEAD", None
        return "ALIVE", fields[19]
    except FileNotFoundError:
        return ("DEAD" if Path("/proc").is_dir() else "UNKNOWN"), None
    except (OSError, IndexError):
        return "UNKNOWN", None


def owner_token_for_process(pid=None):
    pid = os.getpid() if pid is None else pid
    state, birth = process_identity(pid)
    if state != "ALIVE" or birth is None:
        # 无法核对的owner保守保持未知，绝不自动中断。
        birth = "unknown"
    return f"QA_OWNER_V1:{pid}:{birth}:{uuid4()}"


def owner_is_dead(token):
    if not isinstance(token, str):
        return False
    matched = re.fullmatch(r"QA_OWNER_V1:([1-9][0-9]{0,9}):([0-9]{1,30}):([a-f0-9-]{36})", token)
    if matched is None:
        return False
    try:
        UUID(matched[3])
    except ValueError:
        return False
    pid = int(matched[1])
    if pid > 0xFFFFFFFF:
        return False
    state, birth = process_identity(pid)
    return state == "DEAD" or (state == "ALIVE" and birth != matched[2])


OWNER_TOKEN = owner_token_for_process()
