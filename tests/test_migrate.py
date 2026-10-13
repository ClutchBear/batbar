"""数据搬迁的契约（logi-tray → batbar → 绿色模式「跟 exe 同目录」）。

落盘位置换过两代，每一代都要能把上一代的数据接过来。这里锁死 **7 条边界**，
每条都对应一个真实坏结局：

  1. 老位置有、目标无 → 文件必须搬过去（否则电量表清空、菜单闪一下、日志断档）；
  2. **只复制不移动** → 老目录原样留着，用户可回退（2026-10-13 用户确认）；
  3. 幂等 → 再跑一次不出错、不丢数据；
  4. **绝不覆盖** → 目标已有同名文件就跳过（新数据不能被旧数据顶掉）；
  5. **不搬 `agent.pid`** → 旧 PID 若被别的进程复用，重启脚本会误杀无辜；
  6. **最新来源优先** → 两个历史目录都在时，新一代的数据要赢；
  7. 任何异常 → 返回 (False, 原因)，绝不抛（搬迁是保险，不是断言）。

外加两条源码级断言：搬迁必须发生在 `applog.setup()` **之前**（要搬的东西之一
就是日志文件本身），且在 `anchor_cwd()` 之后（路径可能受 cwd 影响）。
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

    def _seed(self, d: Path, log: str = "old log\n",
              last: str = '{"QCY": {"pct": 100, "ts": 1.0}}') -> None:
        """造一份「像真的」数据目录：日志 + 电量表 + pid。"""
        (d / "logs").mkdir(parents=True, exist_ok=True)
        (d / "logs" / "agent.log").write_text(log, encoding="utf-8")
        (d / "last-known.json").write_text(last, encoding="utf-8")
        (d / "agent.pid").write_text("12345", encoding="utf-8")

    # ---- ① 正常搬迁 / ② 只复制不移动 ------------------------------------
    def test_copies_files_into_target(self):
        self._seed(self.old)
        moved, why = M.migrate(self.old, self.new)
        self.assertTrue(moved, why)
        self.assertTrue((self.new / "logs" / "agent.log").is_file())
        self.assertTrue((self.new / "last-known.json").is_file())

    def test_source_is_not_deleted(self):
        """③ 用户明确要「旧目录保留」—— 绿色模式是换个位置再跑一份。"""
        self._seed(self.old)
        M.migrate(self.old, self.new)
        self.assertTrue((self.old / "last-known.json").is_file(), "源目录不能被删")
        self.assertTrue((self.old / "logs" / "agent.log").is_file())

    def test_preserves_content_bytes(self):
        """复制不是重写：字节必须一模一样（电量表里是 UTF-8 设备名）。"""
        self._seed(self.old)
        src = (self.old / "last-known.json").read_bytes()
        M.migrate(self.old, self.new)
        self.assertEqual((self.new / "last-known.json").read_bytes(), src)

    def test_rotated_logs_come_along(self):
        """滚动日志（agent.log.1/.2）也要一起搬 —— 排障看的是历史。"""
        self._seed(self.old)
        (self.old / "logs" / "agent.log.1").write_text("older\n", encoding="utf-8")
        M.migrate(self.old, self.new)
        self.assertTrue((self.new / "logs" / "agent.log.1").is_file())

    # ---- ⑤ 不搬 agent.pid ----------------------------------------------
    def test_pid_file_is_not_migrated(self):
        self._seed(self.old)
        M.migrate(self.old, self.new)
        self.assertFalse((self.new / "agent.pid").exists(),
                         "PID 是瞬时状态，搬过来可能误杀被复用的 PID")

    # ---- ③ 幂等 --------------------------------------------------------
    def test_second_run_is_noop(self):
        self._seed(self.old)
        M.migrate(self.old, self.new)
        moved, why = M.migrate(self.old, self.new)
        self.assertFalse(moved)
        self.assertIn("早已搬过", why)
        self.assertTrue((self.new / "last-known.json").is_file())

    # ---- ④ 绝不覆盖 ----------------------------------------------------
    def test_existing_target_file_wins(self):
        """目标已有 `last-known.json` → 保留目标的；其余文件照常搬。"""
        self._seed(self.old, last="OLD")
        self.new.mkdir(parents=True)
        (self.new / "last-known.json").write_text("NEW", encoding="utf-8")

        moved, why = M.migrate(self.old, self.new)
        self.assertTrue(moved, why)
        self.assertEqual((self.new / "last-known.json").read_text(encoding="utf-8"),
                         "NEW", "目标里的新数据不能被旧数据覆盖")
        # 只有日志是搬过来的（电量表被跳过）
        self.assertIn("logs/agent.log", why)
        self.assertNotIn("last-known.json", why)

    def test_existing_target_log_wins(self):
        self._seed(self.old, log="OLD LOG\n")
        self.new.mkdir(parents=True)
        (self.new / "logs").mkdir()
        (self.new / "logs" / "agent.log").write_text("NEW LOG\n", encoding="utf-8")
        M.migrate(self.old, self.new)
        self.assertEqual((self.new / "logs" / "agent.log").read_text(encoding="utf-8"),
                         "NEW LOG\n")

    # ---- ⑥ 最新来源优先 ------------------------------------------------
    def test_newest_legacy_source_wins(self):
        """两个历史目录都在 → `batbar`（上一代）的数据必须赢过 `logi-tray`。"""
        base = self.tmp / "appdata"
        d_old, d_mid = base / "logi-tray", base / "batbar"
        self._seed(d_old, last="FROM-LOGI-TRAY")
        self._seed(d_mid, last="FROM-BATBAR")
        target = self.tmp / "green"

        with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": str(base)}):
            moved, why = M.migrate(None, target)
        self.assertTrue(moved, why)
        self.assertEqual((target / "last-known.json").read_text(encoding="utf-8"),
                         "FROM-BATBAR")

    # ---- 边界：目录不存在 ----------------------------------------------
    def test_missing_source_is_noop(self):
        moved, why = M.migrate(self.old, self.new)
        self.assertFalse(moved)
        self.assertIn("没有可迁移", why)
        self.assertFalse(self.new.exists(), "不该凭空造出目标目录")

    def test_same_path_is_noop(self):
        """源码运行的默认情形：目标就是 `%LOCALAPPDATA%\\batbar` 自己。"""
        self._seed(self.old)
        moved, why = M.migrate(self.old, self.old)
        self.assertFalse(moved)
        self.assertIn("无需迁移", why)

    # ---- ⑦ 失败不致命 --------------------------------------------------
    def test_copy_failure_returns_false_not_raise(self):
        self._seed(self.old)
        with unittest.mock.patch.object(M.shutil, "copy2",
                                        side_effect=OSError("denied")):
            moved, why = M.migrate(self.old, self.new)
        self.assertFalse(moved)
        self.assertIn("没有可迁移", why)
        self.assertTrue((self.old / "last-known.json").is_file())

    def test_no_localappdata_is_noop(self):
        with unittest.mock.patch.dict(os.environ,
                                      {"LOCALAPPDATA": "", "APPDATA": ""}):
            moved, why = M.migrate()
        self.assertFalse(moved)
        self.assertIn("拿不到", why)

    # ---- 默认来源表 ----------------------------------------------------
    def test_legacy_dirs_oldest_first(self):
        base = self.tmp / "appdata"
        with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": str(base)}):
            dirs = M.legacy_dirs()
        self.assertEqual([d.name for d in dirs], ["logi-tray", "batbar"],
                         "表本身是「最老 → 上一代」的顺序")
        self.assertEqual(M.LEGACY_DIRNAMES, ("logi-tray", "batbar"))


class MigrateIsWiredBeforeLogSetup(unittest.TestCase):
    """源码级：搬迁必须早于 applog.setup()，否则搬不到日志文件本身。"""

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
                        "搬迁必须在 applog.setup() 之前 —— 它要搬的就是日志文件")

    def test_migrate_after_cwd_anchor(self):
        body = self.src[self.src.index("def main("):]
        self.assertLess(body.index("anchor_cwd()"), body.index("migrate.migrate()"))

    def test_migrate_after_arg_parse(self):
        """★ 搬迁必须晚于 `parse_args()` —— `--help` 不许有落盘副作用。

        2026-10-14 CI 实录：冒烟步骤拿 `--help` 真跑一次 exe，绿色模式的
        数据目录正是 exe 所在目录（= 发布目录），搬迁随即把 CI 机器上的历史
        存档复制进发布目录并被打进 zip。把搬迁挪到 `parse_args()` 之后，
        argparse 遇到 `--help` / 参数错误会直接 exit，就轮不到搬迁上场了。
        """
        body = self.src[self.src.index("def main("):]
        self.assertLess(body.index("args = ap.parse_args()"),
                        body.index("migrate.migrate()"),
                        "搬迁必须晚于 parse_args()，否则 --help 也会产生落盘副作用")


if __name__ == "__main__":
    unittest.main()
