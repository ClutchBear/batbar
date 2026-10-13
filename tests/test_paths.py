"""落盘位置（绿色模式）的契约 —— `paths.py` 是**唯一真相源**。

背景：2026-10-13 之前 `applog` / `lastknown` / `singleinstance` / `migrate`
四家各自拼一遍 `%LOCALAPPDATA%\\batbar`。当天改成绿色模式（数据跟着 exe 走），
顺手把落点收进一个模块。这里锁死的是**行为契约 + 单一真相源**：

  * 优先级：`BATBAR_DATA_DIR` > exe 同目录（打包）> `%LOCALAPPDATA%\\batbar`；
  * ★ **exe 目录不可写必须回退**，绝不因为不可写就不落盘（没日志的托盘程序
    等于瞎跑）；
  * `logs` / `last-known.json` / `agent.pid` 都从同一个 data_dir 派生 —— 任何
    模块自己再拼一次路径，都算回归（有源码扫描用例守着）。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from batbar import applog, lastknown, paths, singleinstance

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "batbar"


class DataDirResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="batbar-paths-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        paths.reset_cache()
        self.addCleanup(paths.reset_cache)
        # 默认把 LOCALAPPDATA 指到临时目录，别碰真机
        self.appdata = self.tmp / "appdata"
        self.appdata.mkdir()
        p = unittest.mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(self.appdata), "APPDATA": str(self.appdata),
             "BATBAR_LOG_DIR": "", "BATBAR_DATA_DIR": ""})
        p.start()
        self.addCleanup(p.stop)

    # ---- 优先级 ① 环境变量 --------------------------------------------
    def test_env_override_wins(self):
        target = self.tmp / "custom"
        with unittest.mock.patch.dict(os.environ, {"BATBAR_DATA_DIR": str(target)}):
            paths.reset_cache()
            self.assertEqual(paths.data_dir(), target)
            self.assertIn("BATBAR_DATA_DIR", paths.note())

    def test_env_override_derivatives(self):
        target = self.tmp / "custom"
        with unittest.mock.patch.dict(os.environ, {"BATBAR_DATA_DIR": str(target)}):
            paths.reset_cache()
            self.assertEqual(paths.logs_dir(), target / "logs")
            self.assertEqual(paths.lastknown_path(), target / "last-known.json")
            self.assertEqual(paths.pid_path(), target / "agent.pid")

    # ---- 优先级 ② 打包后 = exe 同目录 ---------------------------------
    def test_frozen_uses_exe_dir(self):
        """绿色模式的核心：数据落在 batbar.exe 旁边。"""
        exe_dir = self.tmp / "portable"
        exe_dir.mkdir()
        with unittest.mock.patch.object(sys, "frozen", True, create=True), \
                unittest.mock.patch.object(sys, "executable", str(exe_dir / "batbar.exe")):
            paths.reset_cache()
            self.assertEqual(paths.data_dir(), exe_dir)
            self.assertIn("绿色模式", paths.note())
            self.assertEqual(paths.logs_dir(), exe_dir / "logs")

    def test_frozen_unwritable_falls_back(self):
        """★ exe 目录不可写（解压到 Program Files 之类）⇒ 回退用户目录。"""
        blocker = self.tmp / "not-a-dir"       # 拿文件冒充目录 → mkdir 必失败
        blocker.write_text("x", encoding="utf-8")
        with unittest.mock.patch.object(sys, "frozen", True, create=True), \
                unittest.mock.patch.object(sys, "executable",
                                           str(blocker / "batbar.exe")):
            paths.reset_cache()
            self.assertEqual(paths.data_dir(), self.appdata / "batbar")
            self.assertIn("不可写", paths.note())

    # ---- 优先级 ③ 源码运行 --------------------------------------------
    def test_source_run_uses_localappdata(self):
        with unittest.mock.patch.object(sys, "frozen", False, create=True):
            paths.reset_cache()
            self.assertEqual(paths.data_dir(), self.appdata / "batbar")

    def test_falls_back_to_home_without_appdata(self):
        with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": "", "APPDATA": ""}), \
                unittest.mock.patch.object(sys, "frozen", False, create=True):
            paths.reset_cache()
            self.assertEqual(paths.data_dir(),
                             Path(os.path.expanduser("~")) / paths.APP_DIR)

    # ---- 可写性探测本身 ----------------------------------------------
    def test_writable_true_and_probe_cleaned(self):
        d = self.tmp / "w"
        self.assertTrue(paths._writable(d))
        self.assertEqual(list(d.iterdir()), [], "探针文件必须删干净")

    def test_writable_false_on_file_path(self):
        f = self.tmp / "f"
        f.write_text("x", encoding="utf-8")
        self.assertFalse(paths._writable(f / "sub"))

    # ---- 缓存 ---------------------------------------------------------
    def test_env_change_takes_effect_immediately(self):
        """环境变量是显式意图，改了就该立刻生效（不必等 reset）。"""
        first = paths.data_dir()
        other = self.tmp / "other"
        with unittest.mock.patch.dict(os.environ, {"BATBAR_DATA_DIR": str(other)}):
            self.assertNotEqual(paths.data_dir(), first)
            self.assertEqual(paths.data_dir(), other)

    def test_auto_result_is_cached_until_inputs_change(self):
        """自动推导会命中缓存 —— 但**输入变了就该失效**（签名缓存，非裸值）。"""
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir(), b.mkdir()
        with unittest.mock.patch.object(sys, "frozen", True, create=True):
            with unittest.mock.patch.object(sys, "executable", str(a / "batbar.exe")):
                self.assertEqual(paths.data_dir(), a)
            # exe 换了位置 = 输入变了 ⇒ 必须重新推导，不能吃旧缓存
            with unittest.mock.patch.object(sys, "executable", str(b / "batbar.exe")):
                self.assertEqual(paths.data_dir(), b)
            paths.reset_cache()
            with unittest.mock.patch.object(sys, "executable", str(a / "batbar.exe")):
                self.assertEqual(paths.data_dir(), a)


class SingleSourceOfTruth(unittest.TestCase):
    """三个模块的落点必须都是 paths 派生的 —— 别再各自拼一遍。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="batbar-sot-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        paths.reset_cache()
        self.addCleanup(paths.reset_cache)
        p = unittest.mock.patch.dict(
            os.environ,
            {"BATBAR_DATA_DIR": str(self.tmp), "BATBAR_LOG_DIR": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_all_three_derive_from_data_dir(self):
        self.assertEqual(applog.default_dir(), self.tmp / "logs")
        self.assertEqual(lastknown.path(), self.tmp / "last-known.json")
        self.assertEqual(singleinstance.pid_path(), self.tmp / "agent.pid")

    def test_log_dir_env_still_overrides_logs_only(self):
        """既有约定 `BATBAR_LOG_DIR` 仍能单独改日志目录（测试/便携部署用）。"""
        other = self.tmp / "logs-elsewhere"
        with unittest.mock.patch.dict(os.environ, {"BATBAR_LOG_DIR": str(other)}):
            self.assertEqual(applog.default_dir(), other)
            # 且 PID 跟着日志目录走 —— 这是老约定的既有行为，别悄悄改掉
            self.assertEqual(singleinstance.pid_path(), other / "agent.pid")
            # 电量表不受影响，仍跟着数据目录
            self.assertEqual(lastknown.path(), self.tmp / "last-known.json")

    def test_lastknown_env_still_overrides(self):
        other = self.tmp / "lk"
        with unittest.mock.patch.dict(os.environ, {"BATBAR_LASTKNOWN": str(other)}):
            self.assertEqual(lastknown.path(), other / "last-known.json")


class NoModuleRebuildsPathsOnItsOwn(unittest.TestCase):
    """源码扫描：**真正读 `%LOCALAPPDATA%` 环境变量的地方**只允许两处。

    paths.py —— 统一的推导逻辑；migrate.py —— 它要**找**历史目录，必须自己拼。
    别处再出现一次，就说明有人绕过 paths 另起了一套（正是这次要消灭的坏味道）。

    注意判据是「代码里读环境变量」而不是「出现过 LOCALAPPDATA 这个词」：
    docstring 里解释落点（含 winproc.py 说的 App Execution Alias 路径）不算犯规。
    """

    ALLOWED = {"paths.py", "migrate.py"}
    PAT = re.compile(r"""environ(?:\.get)?[\[(]\s*['"]LOCALAPPDATA"""
                     r"""|getenv\(\s*['"]LOCALAPPDATA""")

    def test_only_allowed_modules_read_localappdata(self):
        offenders = []
        for f in sorted(SRC.glob("*.py")):
            if f.name in self.ALLOWED:
                continue
            if self.PAT.search(f.read_text(encoding="utf-8")):
                offenders.append(f.name)
        self.assertEqual(offenders, [],
                         "这些模块自己拼了 %LOCALAPPDATA%，应改走 paths.py")

    def test_allowed_modules_really_do_read_it(self):
        """反向断言：免得哪天 paths 改走别的机制、这条扫描变成永远为真的空转。"""
        for name in self.ALLOWED:
            self.assertTrue(self.PAT.search((SRC / name).read_text(encoding="utf-8")),
                            f"{name} 应仍在读 LOCALAPPDATA（扫描白名单该更新了）")

    def test_paths_module_is_dependency_free(self):
        """paths 是最底层模块，不该 import 本项目其它模块（避免循环依赖）。"""
        txt = (SRC / "paths.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", txt)
        self.assertNotIn("from . import", txt)


if __name__ == "__main__":
    unittest.main()
