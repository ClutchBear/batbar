"""旧数据目录自动迁移的契约（2026-10-13 改名 logi-tray → batbar）。

改名后落盘目录从 `%LOCALAPPDATA%\\logi-tray\\` 换到 `%LOCALAPPDATA%\\batbar\\`。
这里锁死迁移的**四条边界**，每一条都对应一个真实坏结局：

  1. 旧目录有、新目录无 → 必须整棵搬过去（否则电量表清空、菜单闪一下）；
  2. 旧目录无 → 静默 no-op（全新用户；也是「已经搬过」的第二次启动）；
  3. 两边都在 → **绝不合并覆盖**（宁可不动，也不能把新版数据写坏）；
  4. 任何异常 → 返回 (False, 原因)，绝不抛（迁移是保险，不是断言）。

外加一条源码级断言：迁移必须发生在 `applog.setup()` **之前** —— 要搬的东西
之一就是日志目录本身，顺序反了就等于没搬日志。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from batbar import migrate as M

ROOT = Path(__file__).resolve().parents[1]


class MigrateBehavior(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="batbar-mig-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.old = self.tmp / "logi-tray"
        self.new = self.tmp / "batbar"

    def _seed_old(self) -> None:
        """造一份「像真的」旧目录：日志 + 电量表 + pid。"""
        (self.old / "logs").mkdir(parents=True)
        (self.old / "logs" / "agent.log").write_text("old log\n", encoding="utf-8")
        (self.old / "last-known.json").write_text(
            '{"QCY": {"pct": 100, "ts": 1.0}}', encoding="utf-8")
        (self.old / "agent.pid").write_text("12345", encoding="utf-8")

    # ---- ① 正常搬迁 ---------------------------------------------------
    def test_moves_whole_tree(self):
        self._seed_old()
        moved, why = M.migrate(self.old, self.new)
        self.assertTrue(moved, why)
        self.assertFalse(self.old.exists(), "旧目录应被整体移走")
        self.assertTrue((self.new / "logs" / "agent.log").is_file())
        self.assertTrue((self.new / "last-known.json").is_file())
        self.assertTrue((self.new / "agent.pid").is_file())

    def test_preserves_content_bytes(self):
        """搬运不是重写：字节必须一模一样（电量表里是 UTF-8 设备名）。"""
        self._seed_old()
        src = (self.old / "last-known.json").read_bytes()
        M.migrate(self.old, self.new)
        self.assertEqual((self.new / "last-known.json").read_bytes(), src)

    def test_second_run_is_noop(self):
        """幂等：搬完再跑一次不能出错，也不能把新数据弄丢。"""
        self._seed_old()
        M.migrate(self.old, self.new)
        moved, why = M.migrate(self.old, self.new)
        self.assertFalse(moved)
        self.assertTrue((self.new / "last-known.json").is_file())
        self.assertIn("旧目录不存在", why)

    # ---- ② 旧目录不存在 -----------------------------------------------
    def test_no_old_dir_is_noop(self):
        moved, why = M.migrate(self.old, self.new)
        self.assertFalse(moved)
        self.assertIn("旧目录不存在", why)
        self.assertFalse(self.new.exists(), "不该凭空造出新目录")

    # ---- ③ 两边都在：绝不合并 -----------------------------------------
    def test_existing_new_dir_is_never_merged(self):
        self._seed_old()
        self.new.mkdir(parents=True)
        (self.new / "last-known.json").write_text("NEW", encoding="utf-8")

        moved, why = M.migrate(self.old, self.new)
        self.assertFalse(moved)
        self.assertIn("不合并", why)
        # 新版数据原封不动
        self.assertEqual((self.new / "last-known.json").read_text(encoding="utf-8"),
                         "NEW")
        # 旧版数据也原封不动（留给人肉处理）
        self.assertTrue((self.old / "last-known.json").is_file())

    # ---- ④ 失败不致命 -------------------------------------------------
    def test_move_failure_returns_false_not_raise(self):
        self._seed_old()
        with unittest.mock.patch.object(M.shutil, "move",
                                        side_effect=OSError("denied")):
            moved, why = M.migrate(self.old, self.new)
        self.assertFalse(moved)
        self.assertIn("迁移失败", why)
        # 源目录仍在（没搬成功就不该假装搬了）
        self.assertTrue((self.old / "last-known.json").is_file())

    def test_same_path_is_skipped(self):
        self._seed_old()
        moved, _why = M.migrate(self.old, self.old)
        self.assertFalse(moved)

    # ---- 默认目录推导 -------------------------------------------------
    def test_default_dirs_follow_localappdata(self):
        """不传参时走 %LOCALAPPDATA%，且命名是 logi-tray → batbar。"""
        fake = self.tmp / "appdata"
        old = fake / "logi-tray"
        old.mkdir(parents=True)
        (old / "agent.pid").write_text("1", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": str(fake)}):
            moved, why = M.migrate()
        self.assertTrue(moved, why)
        self.assertTrue((fake / "batbar" / "agent.pid").is_file())

    def test_no_localappdata_is_noop(self):
        with unittest.mock.patch.dict(os.environ,
                                      {"LOCALAPPDATA": "", "APPDATA": ""}):
            moved, why = M.migrate()
        self.assertFalse(moved)
        self.assertIn("拿不到", why)

    def test_dirnames_are_expected(self):
        self.assertEqual(M.OLD_DIRNAME, "logi-tray")
        self.assertEqual(M.NEW_DIRNAME, "batbar")


class MigrateIsWiredBeforeLogSetup(unittest.TestCase):
    """源码级：迁移必须早于 applog.setup()，否则搬不到日志目录本身。"""

    def setUp(self):
        self.src = (ROOT / "src" / "batbar" / "agent.py").read_text(encoding="utf-8")

    def test_migrate_called_in_main(self):
        self.assertEqual(self.src.count("migrate.migrate()"), 1)

    def test_migrate_precedes_log_setup(self):
        body = self.src[self.src.index("def main("):]
        i_mig = body.index("migrate.migrate()")
        # 用带参数的完整调用做锚点：注释里也出现过 "applog.setup()" 字样，
        # 只匹配 "applog.setup(" 会命中注释行（踩过）。
        i_log = body.index('applog.setup("agent"')
        self.assertLess(i_mig, i_log,
                        "迁移必须在 applog.setup() 之前 —— 它要搬的就是日志目录")

    def test_migrate_after_cwd_anchor(self):
        body = self.src[self.src.index("def main("):]
        self.assertLess(body.index("anchor_cwd()"), body.index("migrate.migrate()"))


if __name__ == "__main__":
    unittest.main()
