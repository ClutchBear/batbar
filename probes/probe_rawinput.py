# -*- coding: utf-8 -*-
"""RAWINPUT 鼠标唤醒检测探针（2026-10-13）。

── 要回答的问题 ──
ARCHITECTURE §6 待办「鼠标硬件唤醒检测」打算用 `RAWINPUT + RIDEV_INPUTSINK`
替代「HID++ 轮询猜测鼠标睡没睡」。但在动手改 agent 之前，必须先实测三件事：

  Q1. 隐藏消息窗口（不注册任何 visible 窗口）能否收到 `WM_INPUT`？
  Q2. `RIDEV_INPUTSINK` 让**后台无焦点**窗口也能收鼠标输入吗？（这是关键：
      agent 常驻托盘、永远没有焦点）
  Q3. 收到的输入能不能**区分"鼠标移动"和"只是路过的键盘/其它 HID"**？
      （本项目只关心罗技鼠标的活动）

── 设计 ──
跑一个隐藏窗口，跑 N 秒，期间提示用户「动一下鼠标」。窗口按
`RIDEV_INPUTSINK` 注册鼠标（UsagePage=GENERIC_DESKTOP=0x01, Usage=MOUSE=0x02），
统计收到的 WM_INPUT 条数 + 每条的类型（移动/按键/滚轮）。

用法：
    .venv\\Scripts\\python.exe -X utf8 probes\\probe_rawinput.py            # 12s
    .venv\\Scripts\\python.exe -X utf8 probes\\probe_rawinput.py --seconds 20
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ── RAWINPUT 常量 ──
RIDEV_INPUTSINK = 0x00000100  # ★ 关键：让无焦点/后台窗口也能收输入
RIDEV_REMOVE = 0x00000001
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RIM_TYPEHID = 2

WM_INPUT = 0x00FF
WM_DESTROY = 2

HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_MOUSE = 0x02
HID_USAGE_GENERIC_KEYBOARD = 0x06

# MOUSE_INPUT_DATA 的 usFlags
MOUSE_MOVE_RELATIVE = 0x00
MOUSE_MOVE_ABSOLUTE = 0x01
MOUSE_VIRTUAL_DESKTOP = 0x02
MOUSE_ATTRIBUTES_CHANGED = 0x04
MOUSE_MOVE_NOCOALESCE = 0x08
MOUSE_BUTTON_1_DOWN = 0x0001
MOUSE_BUTTON_1_UP = 0x0002
MOUSE_BUTTON_2_DOWN = 0x0004
MOUSE_BUTTON_2_UP = 0x0008
MOUSE_BUTTON_3_DOWN = 0x0010
MOUSE_BUTTON_3_UP = 0x0020
MOUSE_BUTTON_4_DOWN = 0x0040
MOUSE_BUTTON_4_UP = 0x0080
MOUSE_BUTTON_5_DOWN = 0x0100
MOUSE_BUTTON_5_UP = 0x0200
MOUSE_WHEEL = 0x0400
MOUSE_HWHEEL = 0x0800


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.c_ushort),
        ("usUsage", ctypes.c_ushort),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


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


def _mouse_kind(flags: int) -> str:
    """把 MOUSEINPUT.dwFlags 翻成人话。"""
    if flags & MOUSE_WHEEL:
        return "滚轮V"
    if flags & MOUSE_HWHEEL:
        return "滚轮H"
    for i, (dn, up) in enumerate(
        [
            (MOUSE_BUTTON_1_DOWN, MOUSE_BUTTON_1_UP),
            (MOUSE_BUTTON_2_DOWN, MOUSE_BUTTON_2_UP),
            (MOUSE_BUTTON_3_DOWN, MOUSE_BUTTON_3_UP),
            (MOUSE_BUTTON_4_DOWN, MOUSE_BUTTON_4_UP),
            (MOUSE_BUTTON_5_DOWN, MOUSE_BUTTON_5_UP),
        ],
        start=1,
    ):
        if flags & dn:
            return f"按键{i}↓"
        if flags & up:
            return f"按键{i}↑"
    if flags & MOUSE_ATTRIBUTES_CHANGED:
        return "属性变化"
    return "移动" if (flags & 0x0F) in (MOUSE_MOVE_RELATIVE, MOUSE_MOVE_ABSOLUTE) else f"移动"


def main() -> int:
    ap = argparse.ArgumentParser(description="RAWINPUT 鼠标唤醒检测探针")
    ap.add_argument("--seconds", type=float, default=12.0)
    args = ap.parse_args()

    stats = {"count": 0, "by_kind": {}, "by_type": {}, "sink_ok": None}
    ignoring = []  # 持 WNDPROC 强引用防 GC
    stop = threading.Event()

    # GetRawInputData 签名（64 位必须显式声明）
    user32.GetRawInputData.restype = wintypes.UINT
    user32.GetRawInputData.argtypes = (
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.UINT),
        wintypes.UINT,
    )

    def _wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            # 先问大小，再取数据（标准两步法）
            size = wintypes.UINT(0)
            n = user32.GetRawInputData(
                wintypes.HANDLE(lparam), RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER)
            )
            if n != 0 or size.value == 0:
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            buf = ctypes.create_string_buffer(size.value)
            n = user32.GetRawInputData(
                wintypes.HANDLE(lparam), RID_INPUT, buf, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER)
            )
            if n == 0xFFFFFFFF:
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            hdr = RAWINPUTHEADER.from_buffer_copy(buf.raw[: ctypes.sizeof(RAWINPUTHEADER)])
            stats["count"] += 1
            tname = {RIM_TYPEMOUSE: "mouse", RIM_TYPEKEYBOARD: "kbd", RIM_TYPEHID: "hid"}.get(
                hdr.dwType, f"type{hdr.dwType}"
            )
            stats["by_type"][tname] = stats["by_type"].get(tname, 0) + 1
            if hdr.dwType == RIM_TYPEMOUSE:
                mi = MOUSEINPUT.from_buffer_copy(
                    buf.raw[
                        ctypes.sizeof(RAWINPUTHEADER) : ctypes.sizeof(RAWINPUTHEADER)
                        + ctypes.sizeof(MOUSEINPUT)
                    ]
                )
                k = _mouse_kind(mi.dwFlags)
                stats["by_kind"][k] = stats["by_kind"].get(k, 0) + 1
            return 0  # WM_INPUT 必须返回 0（DefWindowProc 会做清理）
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(_wnd_proc)
    ignoring.append(proc)

    def _loop():
        dw = user32.DefWindowProcW
        dw.restype = ctypes.c_ssize_t
        dw.argtypes = (wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)
        gm = user32.GetMessageW
        gm.restype = ctypes.c_int
        gm.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint)
        user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        dm = user32.DispatchMessageW
        dm.restype = ctypes.c_ssize_t
        dm.argtypes = (ctypes.POINTER(wintypes.MSG),)
        rc = user32.RegisterClassW
        rc.restype = ctypes.c_ushort
        rc.argtypes = (ctypes.POINTER(WNDCLASS),)
        cw = user32.CreateWindowExW
        cw.restype = wintypes.HWND
        cw.argtypes = (
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
        rr = user32.RegisterRawInputDevices
        rr.restype = wintypes.BOOL
        rr.argtypes = (ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT)

        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        hinst = kernel32.GetModuleHandleW(None)
        kernel32.GetModuleHandleW.restype = None

        wc = WNDCLASS(style=0, lpfnWndProc=proc, hInstance=hinst, lpszClassName="LogiTrayRawInputProbe")
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.GetLastError()
            if err != 1410:  # 1410 = 类已存在（重复跑）
                print(f"[FAIL] RegisterClass rc err={err}")
                stats["sink_ok"] = False
                stop.set()
                return
        # ★ 关键：RIDEV_INPUTSINK —— hwndTarget 指向我们的隐藏窗口，
        #   即使窗口无焦点/不可见，也能收到输入（后台监视的核心）。
        hwnd = user32.CreateWindowExW(0, "LogiTrayRawInputProbe", "", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            print(f"[FAIL] CreateWindow err={ctypes.GetLastError()}")
            stats["sink_ok"] = False
            stop.set()
            return

        rid = RAWINPUTDEVICE()
        rid.usUsagePage = HID_USAGE_PAGE_GENERIC
        rid.usUsage = HID_USAGE_GENERIC_MOUSE
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = hwnd
        ok = user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE))
        stats["sink_ok"] = bool(ok)
        if not ok:
            print(f"[FAIL] RegisterRawInputDevices err={ctypes.GetLastError()}")
            stop.set()
            return
        print("[ok] 隐藏窗口已用 RIDEV_INPUTSINK 注册鼠标 —— 现在动一动鼠标！\n")
        msg = wintypes.MSG()
        while not stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    th = threading.Thread(target=_loop, name="rawinput-probe", daemon=True)
    th.start()

    # 主线程倒计时
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < args.seconds and not stop.is_set():
        left = args.seconds - (time.perf_counter() - t0)
        sys.stdout.write(f"\r  监视中… {left:5.1f}s  WM_INPUT={stats['count']}")
        sys.stdout.flush()
        time.sleep(0.25)
    print("\n")

    print("=== 探针结果 ===")
    print(f"Q1 隐藏窗口收到 WM_INPUT:      {'是 ✓' if stats['count'] > 0 else '否 ✗'}")
    print(f"Q2 RIDEV_INPUTSINK 注册成功:   {'是 ✓' if stats['sink_ok'] else '否 ✗'}")
    print(f"   总条数: {stats['count']}")
    print(f"   按类型: {stats['by_type'] or '（无）'}")
    print(f"   鼠标细分: {stats['by_kind'] or '（无）'}")
    print()
    if stats["count"] > 0:
        print("结论：RAWINPUT + RIDEV_INPUTSINK 可用 —— 后台无焦点也能收鼠标活动，")
        print("      可用来替代「轮询猜鼠标睡没睡」（谁活动 = 谁醒着）。")
        return 0
    print("结论：没收到输入。若刚才确实动了鼠标 ⇒ 该方案在本机不可用，")
    print("      退回 HID++ 轮询判据（`_read_with_second_chance`）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
