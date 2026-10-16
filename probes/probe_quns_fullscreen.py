"""探针：真造一个"全屏窗口"，看 SHQueryUserNotificationState 是否变化。

因为 ① D3D 全屏（exclusive）在纯 Win32 窗口下不一定触发 QUNS_RUNNING_D3D_FULL_SCREEN；
② 无边框全屏往往也不改变 QUNS（它只认"演示模式/独占全屏/忙碌"几种）。
本探针把两条都测：先造覆盖全屏的 WS_POPUP 窗口，再查 QUNS。
跑完自动关窗。
"""

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

user32.SetProcessDPIAware()  # 先 DPI 感知，尺寸才真实

q = shell32.SHQueryUserNotificationState
q.restype = ctypes.c_long
q.argtypes = (ctypes.POINTER(ctypes.c_int),)

NAMES = {
    1: "NOT_PRESENT",
    2: "BUSY",
    3: "D3D_FULL_SCREEN",
    4: "PRESENTATION_MODE",
    5: "ACCEPTS_NOTIFICATIONS",
    6: "QUIET_TIME",
    7: "APP",
}


def quns():
    s = ctypes.c_int(-1)
    q(ctypes.byref(s))
    return s.value


def show_tray_state():
    st = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    print(f"  主屏(DPI感知后) = {st[0]}x{st[1]}")


print(f"建窗之前 QUNS = {quns()} ({NAMES.get(quns(), '?')})")
show_tray_state()

# 造一个覆盖全屏的 WS_POPUP 窗口（borderless fullscreen 的形态）
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
hinst = ctypes.windll.kernel32.GetModuleHandleW(None)

sw = user32.GetSystemMetrics(0)
sh = user32.GetSystemMetrics(1)
hwnd = user32.CreateWindowExW(
    0, "STATIC", "fullscreen-probe", WS_POPUP | WS_VISIBLE, 0, 0, sw, sh, None, None, hinst, None
)
print(f"\n造 borderless 全屏窗口 hwnd={hwnd} ({sw}x{sh})")
time.sleep(1.2)
print(f"  borderless 全屏时 QUNS = {quns()} ({NAMES.get(quns(), '?')})")

# 用户此刻别动键盘鼠标，避免干扰
time.sleep(1.0)
print(f"  再查一次           QUNS = {quns()} ({NAMES.get(quns(), '?')})")

user32.DestroyWindow(hwnd)
time.sleep(0.5)
print(f"\n关窗后 QUNS = {quns()} ({NAMES.get(quns(), '?')})")

print("\n结论提示：")
print(" - 若 borderless 全屏时 QUNS 仍是 5，说明该 API **不认**无边框全屏；")
print(" - 只有独占(D3D)全屏 / 演示模式 / 忙碌 才会变成 2/3/4。")
print(" - 这意味着「无边框全屏游戏」用 QUNS 检测不到，需另配兜底判据。")
