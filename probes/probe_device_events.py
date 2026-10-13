"""探针：验证隐藏消息窗口能否收到 WM_DEVICECHANGE / WM_TASKBARCREATED。

背景：batbar 已有 `LogiTrayThemeWatcher` 隐藏窗口（agent._start_theme_watch），
现在要给它加「设备插拔事件」和「任务栏重建事件」的监听。但有两个技术前提必须
先实测确认，否则方案可能白做：

  1. `WM_DEVICECHANGE`（0x0219）是**目标消息**，默认只发给顶层窗口。隐藏窗口
     （WS_POPUP 且无消息专用）能否收到 DBT_DEVNODES_CHANGED 广播？
     → 需要 `RegisterDeviceNotification` 注册 GUID_DEVINTERFACE_USB_DEVICE
       或监听 `DBT_DEVNODES_CHANGED`（后者理论上广播给所有顶窗）。
  2. `WM_TASKBARCREATED`（RegisterWindowMessage 动态号）能否被隐藏窗口收到？
     → 理论上也是广播给所有顶窗。

本探针建一个隐藏窗口，跑 N 秒，打印收到的所有 WM_DEVICECHANGE 子类型与
WM_TASKBARCREATED。运行期间请插拔 USB 设备 / 重开 explorer 以产生事件。

用法：
    .venv/Scripts/python.exe probes/probe_device_events.py [秒数]
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

_WM_TASKBARCREATED = user32.RegisterWindowMessageW("TaskbarCreated")
print(f"WM_TASKBARCREATED 动态消息号 = 0x{_WM_TASKBARCREATED:04X}")

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class DEV_BROADCAST_DEVICEINTERFACE(ctypes.Structure):
    _fields_ = [("dbcc_size", wintypes.DWORD), ("dbcc_devicetype", wintypes.DWORD),
                ("dbcc_reserved", wintypes.DWORD), ("dbcc_classguid", ctypes.c_ubyte * 16),
                ("dbcc_name", ctypes.c_wchar * 1)]


GUID_DEVINTERFACE_USB_DEVICE = (ctypes.c_ubyte * 16)(*[
    0xA5, 0xDC, 0xBF, 0x9D, 0xE3, 0x8E, 0x11, 0xD0,
    0x94, 0x3F, 0x00, 0xA0, 0xC9, 0x1E, 0xFB, 0x8B])

keeping = []
hits = {"devchange": 0, "taskbar": 0, "arrival": 0, "remove": 0, "nodes": 0}


def _name_of(msg, wparam):
    if msg == _WM_TASKBARCREATED:
        return "WM_TASKBARCREATED"
    if msg == WM_DEVICECHANGE:
        return {DBT_DEVICEARRIVAL: "DBT_DEVICEARRIVAL",
                DBT_DEVICEREMOVECOMPLETE: "DBT_DEVICEREMOVECOMPLETE",
                DBT_DEVNODES_CHANGED: "DBT_DEVNODES_CHANGED"}.get(
                    wparam, f"WM_DEVICECHANGE(0x{wparam:04X})")
    return None


def _wnd_proc(hwnd, msg, wparam, lparam):
    nm = _name_of(msg, wparam)
    if nm:
        ts = time.strftime("%H:%M:%S")
        print(f"  [{ts}] {nm}  (msg=0x{msg:04X} wparam=0x{wparam:04X})", flush=True)
        if msg == _WM_TASKBARCREATED:
            hits["taskbar"] += 1
        elif msg == WM_DEVICECHANGE:
            hits["devchange"] += 1
            if wparam == DBT_DEVICEARRIVAL:
                hits["arrival"] += 1
            elif wparam == DBT_DEVICEREMOVECOMPLETE:
                hits["remove"] += 1
            elif wparam == DBT_DEVNODES_CHANGED:
                hits["nodes"] += 1
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


proc = WNDPROC(_wnd_proc)
keeping.append(proc)

# 签名（64 位句柄截断防护）
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = (wintypes.HWND, ctypes.c_uint,
                                  wintypes.WPARAM, wintypes.LPARAM)
user32.PostQuitMessage.argtypes = (ctypes.c_int,)
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               ctypes.c_uint, ctypes.c_uint)
user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.RegisterClassW.restype = ctypes.c_ushort
user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASS),)
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = (
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
user32.RegisterDeviceNotificationW.restype = wintypes.HANDLE
user32.RegisterDeviceNotificationW.argtypes = (
    wintypes.HWND, wintypes.LPVOID, wintypes.DWORD)
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)

CLASS_NAME = "LogiTrayEvtProbe"
hinst = kernel32.GetModuleHandleW(None)
wc = WNDCLASS(style=0, lpfnWndProc=proc, hInstance=hinst,
              lpszClassName=CLASS_NAME)
if not user32.RegisterClassW(ctypes.byref(wc)):
    print(f"RegisterClass 失败 err={ctypes.GetLastError()}")
    sys.exit(1)

# DBT_DEVTYP_DEVICEINTERFACE = 5
dbi = DEV_BROADCAST_DEVICEINTERFACE()
dbi.dbcc_size = ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE)
dbi.dbcc_devicetype = 5
ctypes.memmove(ctypes.byref(dbi.dbcc_classguid),
               GUID_DEVINTERFACE_USB_DEVICE, 16)
dbi.dbcc_name = ""

# WS_POPUP=0x80000000；不显示（无 WS_VISIBLE）
hwnd = user32.CreateWindowExW(0, CLASS_NAME, "", 0x80000000,
                              0, 0, 0, 0, None, None, hinst, None)
if not hwnd:
    print(f"CreateWindow 失败 err={ctypes.GetLastError()}")
    sys.exit(1)
print(f"隐藏窗口已建 hwnd={hwnd}")

# 注册 USB 设备接口通知（DEVICE_NOTIFY_WINDOW_HANDLE=0）
hn = user32.RegisterDeviceNotificationW(hwnd, ctypes.byref(dbi), 0)
print(f"RegisterDeviceNotification(USB 接口) -> handle={hn} "
      f"(0 = 失败 err={ctypes.GetLastError()})")

dur = int(sys.argv[1]) if len(sys.argv) > 1 else 25
print(f"\n监听 {dur} 秒。请现在【插拔任意 USB 设备】(如接收器/键盘线)，"
      f"并在期间【任务管理器重启 explorer.exe】测试 TaskbarCreated。\n")

msg = wintypes.MSG()
end = time.monotonic() + dur


def _timer():
    time.sleep(dur)
    user32.PostMessageW(hwnd, WM_DESTROY, 0, 0)


threading.Thread(target=_timer, daemon=True).start()
while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))

print(f"\n===== 汇总 =====")
print(f"  WM_DEVICECHANGE 总计 {hits['devchange']} 次")
print(f"    ├ DBT_DEVICEARRIVAL        {hits['arrival']}")
print(f"    ├ DBT_DEVICEREMOVECOMPLETE {hits['remove']}")
print(f"    └ DBT_DEVNODES_CHANGED     {hits['nodes']}")
print(f"  WM_TASKBARCREATED  {hits['taskbar']} 次")
verdict = []
verdict.append("DBT_DEVNODES_CHANGED 可收 ✓" if hits["nodes"] else
               "DBT_DEVNODES_CHANGED 未收到 ✗（可能没插拔）")
verdict.append("WM_TASKBARCREATED 可收 ✓" if hits["taskbar"] else
               "WM_TASKBARCREATED 未收到 ✗（可能没重启 explorer）")
print("  结论: " + " | ".join(verdict))
