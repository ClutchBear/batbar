"""spawn 子进程时抑制控制台黑窗（唯一真相源，2026-10-13）。

── 为什么需要 ──
batbar 打包成 exe 后是 `console=False` 的 **GUI 子系统**程序，进程**没有控制台**。
此时 spawn 任何 **console 子系统**程序（`pwsh.exe` / `powershell.exe`），Windows
会按默认规则**为它新建一个控制台** —— 用户看到黑窗一闪，或（更糟）一个窗口
反复出现。源码 `python agent.py` 跑时进程自带控制台、子进程直接继承，
**永远复现不出这个 bug** —— 只有 exe（或 pythonw）上才暴露。

本机尤其显眼：`shutil.which("pwsh")` 解析到
`%LOCALAPPDATA%\\Microsoft\\WindowsApps\\pwsh.EXE`（0 字节 App Execution Alias），
转发到 Store/MSIX 版 PS7，窗口标题直接显示 MSIX 包路径
`C:\\Program Files\\WindowsApps\\Microsoft.PowerShell_7.6.6.0_x64__8wekyb3d8bbwe\\pwsh.exe`
—— 用户 2026-10-13 截图报的正是这个标题。

── 铁律 ──
**凡是会打包进 GUI 程序的代码，所有 subprocess 一律用 `run_hidden()`。**
`tests/test_no_console_flash.py` 会扫源码守门：`src/batbar/` 下出现
`subprocess.run(` / `subprocess.Popen(` 而没带 creationflags 即测试红。
"""

from __future__ import annotations

import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"

#: CREATE_NO_WINDOW（Win32 0x08000000）：不给子进程分配控制台。
#: 非 Windows 恒为 0，`creationflags=0` 是合法空操作。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0


def hidden_kwargs() -> dict:
    """返回抑制黑窗的 kwargs（非 Windows 返回 `{}`）。

    * `creationflags=CREATE_NO_WINDOW` —— 关键项，不给子进程建控制台；
    * `STARTUPINFO(SW_HIDE)` —— 双保险：万一仍走新建窗口的路径，让它隐藏启动。
    """
    if not IS_WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": si}


def run_hidden(args, **kwargs):
    """`subprocess.run` 的无窗口版（调用方无需再传 creationflags/startupinfo）。"""
    for key, val in hidden_kwargs().items():
        kwargs.setdefault(key, val)
    return subprocess.run(args, **kwargs)
