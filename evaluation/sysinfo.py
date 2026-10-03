"""Environment description and process memory measurement for the evaluation."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from importlib.metadata import version

from healthdata_insight.r_charts import find_rscript


def _windows_counters():
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    get_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    current = ctypes.windll.kernel32.GetCurrentProcess
    current.restype = wintypes.HANDLE
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    if not get_info(current(), ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return counters


def peak_memory_mb() -> dict[str, float] | None:
    """Peak memory of this process: working set (RAM) and commit (private bytes)."""
    if sys.platform == "win32":
        c = _windows_counters()
        return {"peak_working_set_mb": round(c.PeakWorkingSetSize / 2**20, 1),
                "peak_commit_mb": round(c.PeakPagefileUsage / 2**20, 1)}
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"peak_working_set_mb": round(peak / (2**20 if sys.platform == "darwin" else 2**10), 1)}


def free_commit_gb() -> float | None:
    """Memory the OS can still commit (Windows), or available RAM elsewhere; None if unknown."""
    if sys.platform == "win32":
        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return round(status.ullAvailPageFile / 2**30, 2)
    if hasattr(os, "sysconf") and "SC_AVPHYS_PAGES" in os.sysconf_names:
        return round(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30, 2)
    return None


def total_ram_gb() -> float | None:
    if sys.platform == "win32":
        kb = ctypes.c_ulonglong()
        ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kb))
        return round(kb.value / 2**20, 1)
    if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
        return round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30, 1)
    return None


def environment() -> dict:
    rscript = find_rscript()
    r_version = None
    if rscript:
        completed = subprocess.run([str(rscript), "--version"], capture_output=True, text=True, check=False)
        r_version = (completed.stdout or completed.stderr).strip().splitlines()[0] if completed.returncode == 0 else None
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "logical_cpus": os.cpu_count(),
        "total_ram_gb": total_ram_gb(),
        "free_commit_gb_at_start": free_commit_gb(),
        "packages": {name: version(name) for name in ("healthdata-insight", "streamlit", "pandas", "altair")},
        "sqlite": sqlite3.sqlite_version,
        "r": r_version,
        "git_available": shutil.which("git") is not None,
    }
