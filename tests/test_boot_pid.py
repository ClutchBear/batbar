"""启动首帧与快速重启的契约（2026-10-13）。

用户报「双击重启脚本后五六秒才看到托盘图标」。进程内启动实测只要 0.2 秒
（`probes/probe_boot_timing.py`：import 0.10s / 首帧渲染 0.03s），所以耗时在
Python 之外 —— 真凶是重启脚本里 `Get-CimInstance Win32_Process` 的全进程
CIM 枚举（3~8s）。

这里锁死两件事：
  1. 重启脚本**不许再用 CIM 枚举**，改用 agent 自己写的 PID 文件；
  2. PID 文件的写/读行为（含脏数据、不存在、目录重定向）。
"""
from __future__ import annotations

import os
import unittest
import unittest.mock
from pathlib import Path

from batbar import singleinstance as SI

ROOT = Path(__file__).resolve().parents[1]


class PidFile(unittest.TestCase):
    def test_write_read_roundtrip(self):
        p = Path(self._tmp()) / "agent.pid"
        self.assertEqual(SI.write_pid(p), p)
        self.assertEqual(SI.read_pid(p), os.getpid())

    def test_missing_file_is_none(self):
        self.assertIsNone(SI.read_pid(Path(self._tmp()) / "nope.pid"))

    def test_garbage_content_is_none(self):
        p = Path(self._tmp()) / "agent.pid"
        p.write_text("not-a-pid\n", encoding="utf-8")
        self.assertIsNone(SI.read_pid(p))

    def test_empty_content_is_none(self):
        p = Path(self._tmp()) / "agent.pid"
        p.write_text("", encoding="utf-8")
        self.assertIsNone(SI.read_pid(p))

    def test_write_failure_returns_none_not_raise(self):
        """落盘失败（路径是文件/只读）→ 返回 None，绝不把启动拖崩。"""
        blocker = Path(self._tmp()) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        self.assertIsNone(SI.write_pid(blocker / "sub" / "agent.pid"))

    def test_pid_path_follows_log_dir(self):
        """`BATBAR_LOG_DIR` 重定向时 PID 文件跟着搬家（测试/便携部署都要可用）。"""
        import importlib

        from batbar import applog
        with unittest.mock.patch.dict(os.environ, {"BATBAR_LOG_DIR": self._tmp()}):
            importlib.reload(applog)
            # 两边都要 resolve()，不能拿 `pid_path()` 的原始串去比：
            # `pid_path()` 是纯路径拼接、**不做规范化**，而 `Path(...).resolve()`
            # 在 Windows 上会调 GetFinalPathNameByHandle 把 8.3 短名展开成长名。
            # CI 的 runner 上 `%TEMP%` 就是 `C:\Users\RUNNER~1\...`（用户名叫
            # runneradmin，超过 8 字符 ⇒ 系统给了短名），于是「原始串 == 长名」
            # 这个断言在那边**永远不成立**（本机用户名 xin 短，所以一直没暴露）。
            # 本用例要断言的是「PID 跟着日志目录搬家」这个语义，不是字符串形态。
            self.assertEqual(Path(SI.pid_path()).resolve(),
                             Path(self._tmp()).resolve() / "agent.pid")
        importlib.reload(applog)

    def setUp(self):
        import tempfile

        self._tmpdir = tempfile.mkdtemp(prefix="batbar-pid-")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self._tmpdir, ignore_errors=True))

    def _tmp(self) -> str:
        """本用例专属临时目录（同一个用例内多次调用返回同一处）。"""
        return self._tmpdir


class RestartScripts(unittest.TestCase):
    """重启脚本不许退回慢速 CIM 扫描（那正是「五六秒才出图标」的真凶）。"""

    def _scripts(self):
        names = ("restart-diag.bat", "restart-dpi.bat")
        found = [ROOT / n for n in names if (ROOT / n).exists()]
        self.assertTrue(found, "restart scripts missing - did they get renamed?")
        return found

    @staticmethod
    def _code(text: str) -> str:
        """去掉 REM 注释行，只看真正会执行的部分（注释里可以提历史，命令不行）。"""
        return "\n".join(ln for ln in text.splitlines()
                         if not ln.strip().upper().startswith("REM"))

    def test_no_cim_process_walk(self):
        for p in self._scripts():
            text = p.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("Get-CimInstance", self._code(text),
                             f"{p.name} 又用 CIM 全进程枚举了（3~8s）；改用 PID 文件")

    def test_uses_pid_file(self):
        for p in self._scripts():
            text = p.read_text(encoding="utf-8", errors="replace")
            self.assertIn("agent.pid", text,
                          f"{p.name} 应当先读 agent.pid 再 taskkill")

    def test_untrusted_pid_falls_back_to_scan(self):
        """PID 不可信（缺失/陈旧）必须回退扫描，不能直接启动。

        漏杀旧实例 → 新实例被单实例互斥量挡下 → 用户看到「点了没反应」。
        """
        for p in self._scripts():
            code = self._code(p.read_text(encoding="utf-8", errors="replace"))
            self.assertIn("goto fallback", code,
                          f"{p.name} 不可信 PID 时必须 goto fallback 兜底扫描")
            self.assertIn(":fallback", code, f"{p.name} 缺少 :fallback 分支")

    def test_scripts_are_ascii(self):
        """cmd 按 GBK 解码 .bat：混进非 ASCII 会错行拼到下一行命令里。"""
        for p in self._scripts():
            raw = p.read_bytes()
            self.assertTrue(all(b < 128 for b in raw),
                            f"{p.name} 含非 ASCII 字节（cmd 会按 GBK 解码 → 错行）")


class BootTiming(unittest.TestCase):
    def test_boot_marker_never_raises(self):
        from batbar import agent as A

        A._boot("unit-test")


if __name__ == "__main__":
    unittest.main()
