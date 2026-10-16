"""探针 2：定位「隐藏窗口收不到 WM_DEVICECHANGE / WM_TASKBARCREATED」的原因。

探针 1 建了个 WS_POPUP 隐藏窗口，跑 60 秒（用户实测插拔了接收器 + 重启
explorer），**一条事件都没收到**。需要判断是：
  (A) 窗口本身的问题（不可见/样式导致不在广播目标集）
  (B) 是 DBT_DEVNODES_CHANGED 确实不广播 / RegisterDeviceNotification 姿势不对
  (C) 用户操作没生效（可能没插拔成功）

本探针用几种不同窗口样式对比，并额外用 `CreateWindowExW` + WS_OVERLAPPED
（真顶层窗）+ `RegisterDeviceNotification` 双管齐下，同时**主动通知用户**
在运行时插拔。另外加一路：直接把消息循环挂到窗口上（和探针 1 相同）但用
WS_OVERLAPPED 而非 WS_POPUP。

用法：
    .venv/Scripts/python.exe probes/probe_device_events2.py [秒数]
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_DEVICECHANGE = 0x0219
WM_DESTROY = 0x0002
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVNODES_CHANGED = 0x0007
DBT_DEVTYP_DEVICEINTERFACE = 5
DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000

WM_TASKBARCREATED = user32.RegisterWindowMessageW("TaskbarCreated")
print(f"WM_TASKBARCREATED = 0x{WM_TASKBARCREATED:04X}")

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class DEV_BROADCAST_DEVICEINTERFACE(ctypes.Structure):
    _fields_ = [
        ("dbcc_size", wintypes.DWORD),
        ("dbcc_devicetype", wintypes.DWORD),
        ("dbcc_reserved", wintypes.DWORD),
        ("dbcc_classguid", ctypes.c_ubyte * 16),
        ("dbcc_name", ctypes.c_wchar * 1),
    ]


# GUID_DEVINTERFACE_USB_DEVICE {A5DCBF10-6530-11D2-901F-00C04FB951ED}
GUID_USB = (ctypes.c_ubyte * 16)(
    *[0x10, 0xBF, 0xDC, 0xA5, 0x30, 0x65, 0xD2, 0x11, 0x90, 0x1F, 0x00, 0xC0, 0x4F, 0xB9, 0x51, 0xED]
)

keeping = []
hits = {}

user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = (wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)
user32.PostQuitMessage.argtypes = (ctypes.c_int,)
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint)
user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.RegisterClassW.restype = ctypes.c_ushort
user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASS),)
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = (
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
)
user32.RegisterDeviceNotificationW.restype = wintypes.HANDLE
user32.RegisterDeviceNotificationW.argtypes = (wintypes.HWND, wintypes.LPVOID, wintypes.DWORD)
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)

hinst = kernel32.GetModuleHandleW(None)


def _name(msg, wparam):
    if msg == WM_TASKBARCREATED:
        return "WM_TASKBARCREATED"
    if msg == WM_DEVICECHANGE:
        return {
            DBT_DEVICEARRIVAL: "DBT_DEVICEARRIVAL",
            DBT_DEVICEREMOVECOMPLETE: "DBT_DEVICEREMOVECOMPLETE",
            DBT_DEVNODES_CHANGED: "DBT_DEVNODES_CHANGED",
        }.get(wparam, f"WM_DEVICECHANGE(0x{wparam:04X})")
    return None


def make_window(class_name: str, style: int, label: str):
    """建一个窗口 + 注册 USB 设备通知，返回 hwnd。"""

    def _proc(hwnd, msg, wparam, lparam):
        nm = _name(msg, wparam)
        if nm:
            ts = time.strftime("%H:%M:%S")
            print(f"  [{ts}] [{label}] {nm} (msg=0x{msg:04X} wp=0x{wparam:04X})", flush=True)
            hits[label] = hits.get(label, 0) + 1
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    p = WNDPROC(_proc)
    keeping.append(p)
    wc = WNDCLASS(style=0, lpfnWndProc=p, hInstance=hinst, lpszClassName=class_name)
    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = ctypes.GetLastError()
        if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
            print(f"  [{label}] RegisterClass 失败 err={err}")
            return None
    hwnd = user32.CreateWindowExW(0, class_name, "", style, 0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        print(f"  [{label}] CreateWindow 失败 err={ctypes.GetLastError()}")
        return None
    dbi = DEV_BROADCAST_DEVICEINTERFACE()
    dbi.dbcc_size = ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE)
    dbi.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE
    ctypes.memmove(ctypes.byref(dbi.dbcc_classguid), GUID_USB, 16)
    hn = user32.RegisterDeviceNotificationW(hwnd, ctypes.byref(dbi), DEVICE_NOTIFY_WINDOW_HANDLE)
    err = ctypes.GetLastError()
    print(f"  [{label}] hwnd={hwnd} style=0x{style:08X} RegDevNotif={hn} (err={err})")
    keeping.append(dbi)
    return hwnd


# 三种样式对比
w_popup = make_window("LogiProbePopup", 0x80000000, "POPUP隐藏")
w_over = make_window("LogiProbeOver", 0x00CF0000, "OVERLAPPED")  # 真顶层窗
w_tool = make_window("LogiProbeTool", 0x80000000, "POPUP+工具窗")


# 用 Ex 样式加 WS_EX_TOOLWINDOW 的那路另建（Ex 样式没法在 style 里给）
def make_tool_window():
    class_name = "LogiProbeToolEx"
    _proc_ref = None

    def _proc(hwnd, msg, wparam, lparam):
        nm = _name(msg, wparam)
        if nm:
            ts = time.strftime("%H:%M:%S")
            print(f"  [{ts}] [TOOLWINDOW] {nm}", flush=True)
            hits["TOOLWINDOW"] = hits.get("TOOLWINDOW", 0) + 1
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    p = WNDPROC(_proc)
    keeping.append(p)
    wc = WNDCLASS(style=0, lpfnWndProc=p, hInstance=hinst, lpszClassName=class_name)
    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = ctypes.GetLastError()
        if err != 1410:
            return None
    hwnd = user32.CreateWindowExW(0x00000080, class_name, "", 0x80000000, 0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        return None
    dbi = DEV_BROADCAST_DEVICEINTERFACE()
    dbi.dbcc_size = ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE)
    dbi.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE
    ctypes.memmove(ctypes.byref(dbi.dbcc_classguid), GUID_USB, 16)
    hn = user32.RegisterDeviceNotificationW(hwnd, ctypes.byref(dbi), 0)
    print(f"  [TOOLWINDOW] hwnd={hwnd} RegDevNotif={hn} (err={ctypes.GetLastError()})")
    keeping.append(dbi)
    return hwnd


make_tool_window()

dur = int(sys.argv[1]) if len(sys.argv) > 1 else 40
print(f"\n已建 4 个窗口，监听 {dur} 秒。")
print(">>> 请现在【拔掉 USB 接收器，等 3 秒，再插回】<<<\n")


def _timer():
    time.sleep(dur)
    user32.PostMessageW(w_popup or w_over or 0, WM_DESTROY, 0, 0)


threading.Thread(target=_timer, daemon=True).start()
msg = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))

print("\n===== 汇总 =====")
if not hits:
    print("  所有窗口都没收到任何事件 ✗")
else:
    for k, v in hits.items():
        print(f"  {k}: {v} 次")
