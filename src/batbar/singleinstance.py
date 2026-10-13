"""单实例保护：Windows 命名互斥量（CreateMutexW）。

── 为什么必须有这个 ──
两个 agent 实例会各自打开**同一个 HID collection**。而 HID 每个 collection
只有一条输入队列，多个句柄不是「各收一份」，而是**互相抢帧** —— 谁先读到
那一帧，另一方就永远看不到。

这个坑在 2026-10-12 破译 2.4G 接收器协议时反复踩到：现象是「探针脚本收不到
任何帧」，一度被误判成「设备没有实时电量通道」「接收器不可写」，其实只是
托盘 agent 正占着队列。加上本模块后，第二次启动会直接被挡住。

── 作用域 ──
名字用 `Local\\` 前缀 = 当前用户会话。同一会话里以不同身份（普通 / 管理员）
启动的进程**不会被互相挡住**（那是 Windows 的 UAC 完整性级别隔离，符合预期：
普通托盘和管理员调试可以同时存在）。若要全局唯一，改用 `Global\\` 需要
SeCreateGlobalPrivilege，托盘程序没必要。

── 生命周期陷阱 ──
互斥量句柄**必须全程持有**。一旦被 GC 或 CloseHandle，锁立即释放 ——
所以句柄存在模块级全局变量里，绝不放局部变量。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("batbar.single")

MUTEX_NAME = r"Local\batbar-agent"
ERROR_ALREADY_EXISTS = 183
ENV_POPUP = "BATBAR_SINGLETON_POPUP"   # =0 关弹窗：脚本化验证/无人值守时用
PID_NAME = "agent.pid"               # 见 write_pid/read_pid 的注释（重启脚本秒杀用）


def popup_enabled() -> bool:
    return os.environ.get(ENV_POPUP, "1").strip().lower() not in ("0", "false", "no")

# ── Win32 声明 ────────────────────────────────────────────────────────────
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
_k32.ReleaseMutex.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL

_user32 = None  # 惰性加载：只有需要弹窗时才 import user32


def _message_box(title: str, text: str) -> None:
    """弹一个信息框。静默启动（agent-hidden.vbs）时这是唯一能让人看见的反馈。"""
    global _user32
    try:
        if _user32 is None:
            _user32 = ctypes.WinDLL("user32", use_last_error=True)
            _user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                            wintypes.LPCWSTR, wintypes.UINT]
            _user32.MessageBoxW.restype = ctypes.c_int
        MB_OK = 0x00000000
        MB_ICONINFORMATION = 0x00000040
        MB_SETFOREGROUND = 0x00010000      # 不抢焦点，但要让人看见
        _user32.MessageBoxW(None, text, title, MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND)
    except Exception as exc:               # noqa: BLE001 - 弹窗失败无所谓
        log.debug("message box failed: %s", exc)


_handle: int | None = None   # ★ 模块级持有：被 GC 就等于解锁


RETRY_TIMES = 5        # 抢占重试次数
RETRY_DELAY = 0.3      # 每次重试间隔（秒）→ 最多多等 1.5 秒


def acquire(name: str = MUTEX_NAME, notify: bool = True,
            retries: int = RETRY_TIMES, delay: float = RETRY_DELAY) -> bool:
    """抢占单实例锁。

    Returns:
        True  = 抢到了，本进程是唯一实例，继续启动；
        False = 已经有实例在跑，本进程**不应**继续（已提示 + 已记日志）。

    非 Windows 平台一律返回 True（不挡），便于在别处跑测试 / 移植。

    ── 为什么要重试 ──
    上一个进程退出后，内核销毁它的句柄表、释放互斥量**不是同步完成的**
    （实测约 0.5 秒）。不重试的话，「退出托盘 → 立刻重启」会被误判成
    「已有实例在跑」而被拦下 —— 对用户来说就是莫名其妙起不来。
    真有实例常驻时重试多少次都是 busy，所以这个等待不会放行真正的第二个实例。
    """
    global _handle
    if sys.platform != "win32":
        log.debug("non-Windows: single-instance guard skipped")
        return True
    if _handle is not None:                # 同进程重复调用 = 自己持有，放行
        return True

    for attempt in range(retries + 1):
        # ★ 必须先清零：Win32 只在**出错时**设置 last error，成功调用不清零。
        # 不清零的话，上一次调用残留的 ERROR_ALREADY_EXISTS(183) 会让这次明明
        # 抢到锁的调用被误判成「已有实例」—— 表现为莫名其妙起不来。
        ctypes.set_last_error(0)
        h = _k32.CreateMutexW(None, False, name)
        if not h:
            # 拿不到句柄（极罕见）：不挡住程序，只是失去保护
            log.warning("CreateMutexW failed (%s): single-instance guard inactive",
                        ctypes.get_last_error())
            return True
        err = ctypes.get_last_error()
        if err != ERROR_ALREADY_EXISTS:
            _handle = h
            log.debug("single-instance lock acquired: %s", name)
            return True
        # 已经有别人持有了：关掉这个句柄（我们不是 owner），等一下再试
        _k32.CloseHandle(h)
        if attempt < retries:
            log.debug("lock busy, retry %d/%d", attempt + 1, retries)
            time.sleep(delay)

    log.error("已有 batbar 实例在运行，本次启动退出（两个实例会抢同一条 HID 输入队列）")
    if notify and popup_enabled():
        _message_box("batbar",
                     "batbar 已经在运行了（看系统托盘图标）。\n\n"
                     "同时开两个会互相抢 HID 数据，导致电量读数不动，"
                     "所以这次启动被拦下了。\n\n"
                     "要重启的话：先右键托盘图标退出，再启动。")
    return False


def already_running(name: str = MUTEX_NAME) -> bool:
    """只问一句「有没有实例在跑」，不抢锁、不弹窗、不影响本进程状态。

    给 tools/probe_query.py 这类实验脚本用：托盘在跑时探针必然抢帧，
    提前检测比跑完拿到空数据再排查省时间。
    """
    if sys.platform != "win32":
        return False
    if _handle is not None:                # 本进程自己就持有锁
        return True
    ctypes.set_last_error(0)
    h = _k32.CreateMutexW(None, False, name)
    if not h:
        return False
    busy = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    _k32.CloseHandle(h)                    # 探测用句柄立即归还，绝不持有
    return busy


def pid_path() -> Path:
    """PID 文件路径 —— **数据目录**下的 `agent.pid`（落点见 `paths.py`）。

    打包后就是 `batbar.exe` 同目录（绿色模式）；源码运行在
    `%LOCALAPPDATA%\\batbar\\`。既有约定 `BATBAR_LOG_DIR` 仍然生效：
    它一设，PID 就跟去日志目录（测试 / 便携部署用）。
    """
    from . import applog, paths

    env = os.environ.get(applog.ENV_DIR)
    if env:
        # 日志目录被重定向时，PID 就放同一处（不往外跳一级）
        return Path(env) / PID_NAME
    return paths.pid_path()


def write_pid(path: str | os.PathLike[str] | None = None) -> Path | None:
    """把本进程 PID 落盘，供重启脚本秒杀旧实例。

    为什么需要它（2026-10-13）：`restart-diag.bat` 原来用
    `Get-CimInstance Win32_Process` 按命令行找进程再杀 —— WMI/CIM 全进程枚举
    实测 **3~8 秒**，也就是「双击重启脚本后五六秒才看到托盘图标」的真凶
    （agent 进程内启动只要 0.2 秒）。有了 PID 文件，脚本直接
    `taskkill /F /PID <n>`（几十毫秒）即可。

    Returns:
        写入成功的路径；写不了（权限/目录不可写）返回 None —— 落 PID 失败
        不该拖垮启动，脚本会退回慢速兜底。
    """
    try:
        p = Path(path) if path is not None else pid_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(os.getpid()), encoding="utf-8")
        return p
    except Exception as exc:               # noqa: BLE001
        log.debug("write pid failed: %s", exc)
        return None


def read_pid(path: str | os.PathLike[str] | None = None) -> int | None:
    """读 PID 文件；文件不存在/内容不是数字/读不了 → None。"""
    try:
        p = Path(path) if path is not None else pid_path()
        txt = p.read_text(encoding="utf-8").strip()
        return int(txt) if txt.isdigit() else None
    except Exception:                      # noqa: BLE001
        return None


def release() -> None:
    """主动释放锁（正常退出时其实不必调，进程结束内核会自动清理）。

    测试与「同一进程内重启 agent」的场景需要它。
    """
    global _handle
    if _handle is None:
        return
    try:
        _k32.ReleaseMutex(_handle)
        _k32.CloseHandle(_handle)
    except Exception as exc:               # noqa: BLE001
        log.debug("release mutex failed: %s", exc)
    finally:
        _handle = None
