# -*- coding: utf-8 -*-
"""实测 batbar.exe 的常驻内存与 CPU 占用（ctypes 直读，不依赖 psutil）。

为什么用 `GetProcessTimes` 而不是任务管理器的一列数字：
  - 任务管理器的「CPU」是**瞬时采样**，托盘程序大部分时间在等事件，采样点
    撞上轮询就会偏高、撞不上就是 0.00%，两边都不代表真实开销。
  - 这里取的是**一段时间的 CPU 时间增量 ÷ 墙钟时间**，即平均占用率 ——
    这才是「常驻代价」的正确口径。

PyInstaller onefile 会有**两个**进程：bootloader 父进程 + 真正的应用子进程。
两个都要量，加起来才是用户看到的开销（父进程基本只做解压与等待，占用很小）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _ft(f) -> int:
    return (f.dwHighDateTime << 32) | f.dwLowDateTime


def _cpu_seconds(a: int, b: int) -> float:
    """两个 FILETIME 之间的秒数（100ns 单位）。"""
    return ((b - a) & 0xFFFFFFFFFFFFFFFF) / 1e7


def measure(pid: int, seconds: float = 20.0) -> dict:
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        raise RuntimeError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
    try:
        c1, e1, k1, u1 = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        c2, e2, k2, u2 = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        k32.GetProcessTimes(h, ctypes.byref(c1), ctypes.byref(e1), ctypes.byref(k1), ctypes.byref(u1))
        t1 = time.perf_counter()
        time.sleep(seconds)
        k32.GetProcessTimes(h, ctypes.byref(c2), ctypes.byref(e2), ctypes.byref(k2), ctypes.byref(u2))
        t2 = time.perf_counter()

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)

        used = _cpu_seconds(_ft(k1), _ft(k2)) + _cpu_seconds(_ft(u1), _ft(u2))
        wall = t2 - t1
        return {
            "pid": pid,
            "wall": wall,
            "cpu_s": used,
            "pct_of_one_core": 100.0 * used / wall,
            "pct_of_all_cores": 100.0 * used / wall / os.cpu_count(),
            "working_set": pmc.WorkingSetSize,
            "private": pmc.PagefileUsage,
            "peak_ws": pmc.PeakWorkingSetSize,
        }
    finally:
        k32.CloseHandle(h)


def pids_of(name: str) -> list[int]:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH", "/FO", "CSV"], capture_output=True, text=True
    ).stdout
    res = []
    for line in out.splitlines():
        if name.lower() in line.lower():
            try:
                res.append(int(line.split(",")[1].strip('"')))
            except (IndexError, ValueError):
                pass
    return res


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    name = sys.argv[2] if len(sys.argv) > 2 else "batbar.exe"

    pids = pids_of(name)
    if not pids:
        print(f"[X] 没找到运行中的 {name}；先启动它再跑本脚本")
        return 1

    print(f"采样 {seconds:.0f} 秒 · 逻辑核心 {os.cpu_count()} 个 · 进程 {name} PID={pids}")
    print()
    rows = [measure(p, seconds) for p in pids]  # 串行采样，避免互相干扰计时

    MB = 1048576.0
    print(
        f"{'PID':>8}{'工作集(MB)':>12}{'专用(MB)':>10}{'峰值(MB)':>10}"
        f"{'CPU秒/采样':>12}{'单核%':>9}{'全核%':>8}"
    )
    for r in rows:
        print(
            f"{r['pid']:>8}{r['working_set'] / MB:>12.1f}{r['private'] / MB:>10.1f}"
            f"{r['peak_ws'] / MB:>10.1f}{r['cpu_s']:>12.3f}"
            f"{r['pct_of_one_core']:>9.3f}{r['pct_of_all_cores']:>8.3f}"
        )

    tot_ws = sum(r["working_set"] for r in rows)
    tot_pr = sum(r["private"] for r in rows)
    tot_cpu = sum(r["cpu_s"] for r in rows)
    wall = rows[0]["wall"]
    print()
    print(
        f"合计：工作集 {tot_ws / MB:.1f} MB · 专用(提交) {tot_pr / MB:.1f} MB · "
        f"平均 CPU {100.0 * tot_cpu / wall:.3f}%（单核口径）/ "
        f"{100.0 * tot_cpu / wall / os.cpu_count():.3f}%（全核口径）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
