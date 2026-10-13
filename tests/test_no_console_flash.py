"""GUI 进程 spawn 子进程不得弹出控制台黑窗（2026-10-13 用户报的观察 bug）。

打包后的 batbar 是 `console=False` 的 **GUI 子系统**程序，进程**没有控制台**。
此时 spawn console 子系统程序（`pwsh.exe` / `powershell.exe`），Windows 会按
默认规则**为它新建一个控制台窗口** —— 表现为「启动时、随便有动作时，屏幕上
都冒出一个 terminal 窗口」。

实测（`probes/probe_console_flash.py`，用 pythonw.exe 无控制台进程跑，含对照）：

    不带 flag   → 新建 `ConsoleWindowClass`，标题
                  `管理员:C:\\Program Files\\WindowsApps\\Microsoft.PowerShell_7.6.6.0_...\\pwsh.exe`
                  —— 与本机 `shutil.which("pwsh")`
                  （`%LOCALAPPDATA%\\Microsoft\\WindowsApps\\pwsh.EXE` 这个 0 字节
                  App Execution Alias，转发到 Store/MSIX 版 PS7）完全对上
    带 flag     → 新窗口数 **0**
    再跑 naked  → 再次复现（排除偶发）

⚠️ 源码 `python agent.py` 跑时进程自带控制台、子进程直接继承，
**永远复现不出这个 bug** ⇒ 必须由测试守住，不能靠手测。
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from batbar import winproc as W

SRC = Path(__file__).resolve().parents[1] / "src" / "batbar"
SHELL = "pwsh"


class NoConsoleFlash(unittest.TestCase):
    """① 源码扫描守门 + ② winproc 行为验证。"""

    def test_src_never_calls_subprocess_directly(self):
        """src 下**除 winproc.py 外**不得直接调 subprocess.run/Popen。

        约定比「记得带 creationflags」更强：一律走 `winproc.run_hidden()`，
        新增代码连想起这件事的必要都没有。
        """
        offenders = []
        for p in sorted(SRC.rglob("*.py")):
            if p.name == "winproc.py":          # 唯一允许直接调用的地方
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if "subprocess.run(" in line or "subprocess.Popen(" in line:
                    offenders.append("%s:%d" % (p.name, i))
        self.assertEqual(
            offenders, [],
            "这些地方会弹控制台黑窗，请改用 winproc.run_hidden()：%s" % offenders)

    def test_shell_probers_go_through_run_hidden(self):
        """真正 spawn shell 的两个模块（回归钉）。"""
        for name in ("bt_battery.py", "receiver24.py"):
            body = (SRC / name).read_text(encoding="utf-8")
            self.assertIn("winproc.run_hidden(", body, name)
            self.assertNotIn("subprocess.run(", body, name)

    def test_hidden_kwargs_carry_create_no_window(self):
        kw = W.hidden_kwargs()
        if not W.IS_WINDOWS:
            self.assertEqual(kw, {})
            return
        self.assertNotEqual(W.CREATE_NO_WINDOW, 0, "CREATE_NO_WINDOW 常量取不到")
        self.assertEqual(kw["creationflags"], W.CREATE_NO_WINDOW)
        si = kw["startupinfo"]
        self.assertTrue(si.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(si.wShowWindow, subprocess.SW_HIDE)

    def test_run_hidden_injects_flags(self):
        seen = {}

        def fake_run(args, **kwargs):
            seen.update(kwargs)
            seen["_args"] = args
            return "sentinel"

        with mock.patch.object(W.subprocess, "run", fake_run):
            out = W.run_hidden([SHELL, "-NoProfile", "-Command", "1"])
        self.assertEqual(out, "sentinel")
        self.assertEqual(seen["_args"], [SHELL, "-NoProfile", "-Command", "1"])
        if W.IS_WINDOWS:
            self.assertEqual(seen.get("creationflags"), W.CREATE_NO_WINDOW)
            self.assertIn("startupinfo", seen)

    def test_run_hidden_keeps_caller_supplied_flags(self):
        """调用方已显式传了 creationflags / startupinfo 时不得被覆盖。"""
        seen = {}

        def fake_run(args, **kwargs):
            seen.update(kwargs)
            return None

        with mock.patch.object(W.subprocess, "run", fake_run):
            W.run_hidden(["x"], creationflags=123, startupinfo="SI")
        self.assertEqual(seen["creationflags"], 123)
        self.assertEqual(seen["startupinfo"], "SI")

    def test_run_hidden_passes_through_capture_kwargs(self):
        """capture_output / timeout / encoding 这些要原样透传（别被吞掉）。"""
        seen = {}

        def fake_run(args, **kwargs):
            seen.update(kwargs)
            return None

        with mock.patch.object(W.subprocess, "run", fake_run):
            W.run_hidden(["x"], capture_output=True, text=True, timeout=25,
                         encoding="utf-8", errors="replace")
        self.assertTrue(seen["capture_output"])
        self.assertEqual(seen["timeout"], 25)
        self.assertEqual(seen["encoding"], "utf-8")
        self.assertEqual(seen["errors"], "replace")


if __name__ == "__main__":
    unittest.main()
