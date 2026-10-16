"""探针：SHQueryUserNotificationState 在本机的真实返回值。

用途：确认 ① 函数能否调到；② 当前（非全屏）返回什么；③ 各枚举值语义。
纯只读，不改任何系统状态。
"""

import ctypes
from ctypes import wintypes

shell32 = ctypes.windll.shell32
f = shell32.SHQueryUserNotificationState
f.restype = ctypes.c_long
f.argtypes = (ctypes.POINTER(ctypes.c_int),)

NAMES = {
    1: "QUNS_NOT_PRESENT",
    2: "QUNS_BUSY",
    3: "QUNS_RUNNING_D3D_FULL_SCREEN",
    4: "QUNS_PRESENTATION_MODE",
    5: "QUNS_ACCEPTS_NOTIFICATIONS",
    6: "QUNS_QUIET_TIME",
    7: "QUNS_APP",
}

state = ctypes.c_int(-1)
hr = f(ctypes.byref(state))
print(f"HRESULT = {hr:#010x}  (0 = S_OK)")
print(f"state   = {state.value}  ({NAMES.get(state.value, '未知')})")

# 再连查 3 次看是否稳定
print("\n连续 3 次：")
for i in range(3):
    s = ctypes.c_int(-1)
    hr = f(ctypes.byref(s))
    print(f"  #{i + 1} hr={hr:#010x} state={s.value} ({NAMES.get(s.value, '?')})")

# 拿当前前台窗口做个对照（看有没有全屏窗口）
user32 = ctypes.windll.user32
hwnd = user32.GetForegroundWindow()
print(f"\n前台窗口 hwnd = {hwnd}")
if hwnd:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    print(f"  标题 = {buf.value!r}")
    rc = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    print(
        f"  窗口矩形 = ({rc.left},{rc.top})-({rc.right},{rc.bottom})  "
        f"尺寸 {rc.right - rc.left}x{rc.bottom - rc.top}"
    )
    print(f"  主屏 = {sw}x{sh}")
    full = rc.left <= 0 and rc.top <= 0 and rc.right >= sw and rc.bottom >= sh
    print(f"  占满主屏？ {'是' if full else '否'}")
