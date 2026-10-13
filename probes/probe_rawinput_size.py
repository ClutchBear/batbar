# -*- coding: utf-8 -*-
"""RAWINPUT 尺寸探针（2026-10-13）—— 查清 GetRawInputData 的真实尺寸语义。

背景：probe_rawinput.py 第一次跑收到了 322 条 WM_INPUT，但解析鼠标数据时
抛 `Buffer size too small (24 instead of at least 32 bytes)` —— 说明
`pcbSize` 拿到的 24 不是"整条输入的大小"，而是别的什么。本探针在同一回调里
把每一步的尺寸都打出来，定位到底哪个参数写错了。

用法：
    .venv\\Scripts\\python.exe -X utf8 probes\\probe_rawinput_size.py
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0

WM_INPUT = 0x00FF
WM_DESTROY = 2


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", ctypes.c_ushort), ("usUsage", ctypes.c_ushort),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWMOUSE(ctypes.Structure):
    """官方 RAWMOUSE：usFlags(2) + (ulButtons(4) | ulRawButtons(4) 联合)
    + lLastX(4) + lLastY(4) + ulExtraInformation(4) —— 去 padding 后 24 字节。"""
    _fields_ = [("usFlags", ctypes.c_ushort),
                ("_pad", ctypes.c_ushort),          # 对齐到 4
                ("ulButtons", wintypes.ULONG),
                ("ulRawButtons", wintypes.ULONG),
                ("lLastX", wintypes.LONG),
                ("lLastY", wintypes.LONG),
                ("ulExtraInformation", wintypes.ULONG)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


def main() -> int:
    print(f"sizeof(RAWINPUTHEADER) = {ctypes.sizeof(RAWINPUTHEADER)}")
    print(f"sizeof(RAWMOUSE)       = {ctypes.sizeof(RAWMOUSE)}")
    print(f"预期 raw mouse 整条     = "
          f"{ctypes.sizeof(RAWINPUTHEADER) + ctypes.sizeof(RAWMOUSE)}")
    print()

    # 关键：GetRawInputData 的 pcbSize 语义 = **cbSizeHeader 之外的数据大小**
    # 官方签名：UINT GetRawInputData(HRAWINPUT, UINT uiCommand, LPVOID pData,
    #                                PUINT pcbSize, UINT cbSizeHeader);
    # 当 pData=NULL 时，*pcbSize 被写成「整条 RAWINPUT（含 header）的大小」。
    user32.GetRawInputData.restype = wintypes.UINT
    user32.GetRawInputData.argtypes = (wintypes.HANDLE, wintypes.UINT,
                                       wintypes.LPVOID,
                                       ctypes.POINTER(wintypes.UINT),
                                       wintypes.UINT)

    seen = {"n": 0, "sizes": set(), "detail": []}
    keep = []

    def _wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            size = wintypes.UINT(0)
            # 第一次：只要大小（pData=NULL）
            n1 = user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT,
                                        None, ctypes.byref(size),
                                        ctypes.sizeof(RAWINPUTHEADER))
            if n1 != 0 or size.value == 0:
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            seen["sizes"].add(size.value)
            buf = ctypes.create_string_buffer(size.value)
            # ★ 关键修正：pcbSize 传入**当前缓冲大小**（size.value），
            #   而不是别的值 —— 否则函数认为缓冲不够、返回 -1。
            cap = wintypes.UINT(size.value)
            n2 = user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT,
                                        buf, ctypes.byref(cap),
                                        ctypes.sizeof(RAWINPUTHEADER))
            seen["n"] += 1
            if seen["n"] <= 3:
                seen["detail"].append(
                    f"  #{seen['n']} size(q)={size.value} ret={n2} "
                    f"cap={cap.value} err={ctypes.GetLastError()}")
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(_wnd_proc)
    keep.append(proc)

    def _loop():
        dw = user32.DefWindowProcW
        dw.restype = ctypes.c_ssize_t
        dw.argtypes = (wintypes.HWND, ctypes.c_uint, wintypes.WPARAM,
                       wintypes.LPARAM)
        gm = user32.GetMessageW
        gm.restype = ctypes.c_int
        gm.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                       ctypes.c_uint, ctypes.c_uint)
        user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        dm = user32.DispatchMessageW
        dm.restype = ctypes.c_ssize_t
        dm.argtypes = (ctypes.POINTER(wintypes.MSG),)
        rc = user32.RegisterClassW
        rc.restype = ctypes.c_ushort
        rc.argtypes = (ctypes.POINTER(WNDCLASS),)
        cw = user32.CreateWindowExW
        cw.restype = wintypes.HWND
        cw.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                       wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
        rr = user32.RegisterRawInputDevices
        rr.restype = wintypes.BOOL
        rr.argtypes = (ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT,
                       wintypes.UINT)
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        hinst = kernel32.GetModuleHandleW(None)
        kernel32.GetModuleHandleW.restype = None

        wc = WNDCLASS(style=0, lpfnWndProc=proc, hInstance=hinst,
                      lpszClassName="LogiTrayRawSizeProbe")
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, "LogiTrayRawSizeProbe", "", 0,
                                      0, 0, 0, 0, None, None, hinst, None)
        rid = RAWINPUTDEVICE(usUsagePage=0x01, usUsage=0x02,
                             dwFlags=RIDEV_INPUTSINK, hwndTarget=hwnd)
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                       ctypes.sizeof(RAWINPUTDEVICE))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=_loop, name="rawsize", daemon=True).start()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 8.0:
        sys.stdout.write(f"\r  动动鼠标… 剩余 {8.0-(time.perf_counter()-t0):4.1f}s "
                         f"收到={seen['n']}")
        sys.stdout.flush()
        time.sleep(0.25)
    print("\n")
    print("=== 结果 ===")
    print(f"收到条数: {seen['n']}")
    print(f"pcbSize 上报过的值: {sorted(seen['sizes'])}")
    for d in seen["detail"]:
        print(d)
    print()
    print("判读：若 size==size(header)+size(mouse)=40，且 ret==40 ⇒ 尺寸语义对了，")
    print("      MOUSEINPUT 缓冲区就该按 40 字节开；之前失败是因为第二参 pcbSize")
    print("      没传成「缓冲容量」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
