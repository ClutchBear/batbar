"""托盘图标更新到底有没有被 shell 收下？（只读诊断，不改项目代码）

背景：agent 在 +0.6s 就把电量画上去了（日志「首个真读数已画」），但用户
6~7s 后才在托盘看到数字。两种可能：
  A. 早期 NIM_MODIFY 被 shell 拒收（返回值 0）→ 修法是「重试直到收下」
  B. shell 收下了但没重绘 → 修法是换一种呈现方式（重新 add）

本脚本用同一套 pystray 路径复现：add 一帧，然后隔不同时间点改图标，
逐个打印 Shell_NotifyIcon 的返回值 + GetLastError。托盘里会短暂出现一个
名为 "batbar-probe" 的小图标（几秒后自动消失）。
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, "src")

import pystray  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from pystray import _win32 as W  # noqa: E402

kernel32 = ctypes.windll.kernel32
results: list[tuple[float, int, int, int]] = []
_t0 = time.monotonic()

_orig_message = W.Icon._message


def _spy_message(self, code, flags, **kwargs):
    """复刻 pystray 的发送逻辑，但把 Shell_NotifyIcon 的返回值捞出来。"""
    r = W.win32.Shell_NotifyIcon(code, W.win32.NOTIFYICONDATAW(
        cbSize=ctypes.sizeof(W.win32.NOTIFYICONDATAW),
        hWnd=self._hwnd,
        hID=id(self),
        uFlags=flags,
        **kwargs))
    err = kernel32.GetLastError()
    dt = time.monotonic() - _t0
    results.append((dt, code, int(r), err))
    print(f"  +{dt:5.2f}s  code=0x{code:04X} flags=0x{flags:04X} "
          f"-> 返回 {int(r)}  GetLastError={err}")
    return r


W.Icon._message = _spy_message


def _img(n: int | None) -> Image.Image:
    im = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((2, 2, 29, 29), fill=(40, 40, 40, 255))
    if n is None:
        d.line((8, 16, 24, 16), fill=(255, 255, 255, 255), width=4)
    else:
        d.text((9, 9), str(n), fill=(255, 255, 255, 255))
    return im


print("=== add（启动占位：--) ===")
icon = pystray.Icon("batbar-probe", _img(None), "probe: 启动中…",
                    pystray.Menu(pystray.MenuItem("退出", lambda *_: icon.stop())))
icon.run_detached()
time.sleep(0.12)

# 模拟 agent 的真实节奏：+0.3s / +0.6s / +1.5s / +3s / +6s 各改一次图标
for delay, n in ((0.2, 1), (0.3, 2), (0.9, 3), (1.5, 4), (3.0, 5)):
    time.sleep(delay)
    print(f"=== 第 {n} 次改图标（+{time.monotonic() - _t0:.2f}s）===")
    icon.icon = _img(n)
    icon.title = f"probe: {n}0%"
    time.sleep(0.05)

print("\n=== 汇总 ===")
for dt, code, r, err in results:
    kind = {0: "NIM_ADD", 1: "NIM_MODIFY", 2: "NIM_DELETE"}.get(code, hex(code))
    flags = {0x2: "ICON", 0x4: "TIP"}.get(0, "")
    print(f"+{dt:5.2f}s {kind:<11} 返回={r} err={err}")

time.sleep(1.5)
icon.stop()
print("\n探针图标已移除。")
