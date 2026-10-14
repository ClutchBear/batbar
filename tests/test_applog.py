"""applog.py 单测：落盘、幂等、滚动、失败兜底。

每个用例都会动 root logger 的 handler，所以 setUp/tearDown 严格存档还原，
免得污染其它测试的输出。
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from batbar import applog

_MARK = applog._MARK


def _marked(handlers):
    return [h for h in handlers if getattr(h, _MARK, False)]


class Setup(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        self._saved = list(root.handlers)
        self._level = root.level
        for h in list(root.handlers):
            root.removeHandler(h)
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:      # noqa: BLE001
                pass
        for h in self._saved:
            root.addHandler(h)
        root.setLevel(self._level)
        self._tmp.cleanup()

    def _flush(self):
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:      # noqa: BLE001
                pass

    def test_file_created_and_written(self):
        p = applog.setup("ut", log_dir=self.dir)
        assert p is not None
        logging.getLogger("batbar.ut").info("hello-applog")
        self._flush()
        self.assertTrue(p.exists())
        self.assertIn("hello-applog", p.read_text(encoding="utf-8"))

    def test_idempotent_no_duplicate_handlers(self):
        applog.setup("ut", log_dir=self.dir)
        n1 = len(_marked(logging.getLogger().handlers))
        applog.setup("ut", log_dir=self.dir)
        n2 = len(_marked(logging.getLogger().handlers))
        self.assertEqual(n1, n2, "重复 setup 会叠加 handler，导致日志每行重复")

    def test_rotation(self):
        p = applog.setup("ut", log_dir=self.dir, max_bytes=400, backups=1)
        assert p is not None
        lg = logging.getLogger("batbar.ut-rotate")
        for i in range(60):
            lg.info("x" * 40 + f" {i}")
        self._flush()
        self.assertTrue(Path(str(p) + ".1").exists(), "超过 max_bytes 应产生 .1")

    def test_rotation_backup_chain(self):
        """backups=2 ⇒ 只保留 .1 / .2，不该出现 .3（自写 handler 的改名逻辑要对齐标准库）。"""
        p = applog.setup("ut", log_dir=self.dir, max_bytes=200, backups=2)
        assert p is not None
        lg = logging.getLogger("batbar.ut-chain")
        for i in range(60):
            lg.info("y" * 40 + f" {i}")
        self._flush()
        self.assertTrue(Path(str(p) + ".1").exists())
        self.assertTrue(Path(str(p) + ".2").exists())
        self.assertFalse(Path(str(p) + ".3").exists(), "超出 backup_count 的那份必须丢掉")

    def test_rotation_keeps_writable_and_newest_first(self):
        """滚动之后：主文件继续可写，且装的是**最新**那几条。"""
        p = applog.setup("ut", log_dir=self.dir, max_bytes=200, backups=1)
        assert p is not None
        lg = logging.getLogger("batbar.ut-newest")
        for i in range(30):
            lg.info(f"old-{i}")
        self._flush()
        lg.info("marker-newest")
        self._flush()
        text = Path(p).read_text(encoding="utf-8")
        self.assertIn("marker-newest", text, "滚动后主文件必须继续可写")
        self.assertNotIn("old-0", text, "最老的内容应该已经被滚到 .1 里了")

    def test_unwritable_dir_returns_none(self):
        # 用一个「已存在的普通文件」当目录 → mkdir 必失败，但 setup 不能抛
        blocker = self.dir / "iam-a-file"
        blocker.write_text("x", encoding="utf-8")
        p = applog.setup("ut", log_dir=blocker)
        self.assertIsNone(p)
        # 控制台 handler 仍然装上了（日志不至于全丢）
        self.assertTrue(any(isinstance(h, logging.StreamHandler) for h in _marked(
            logging.getLogger().handlers)))


class Level(unittest.TestCase):
    def setUp(self):
        self._env = applog.os.environ.get(applog.ENV_DEBUG)

    def tearDown(self):
        if self._env is None:
            applog.os.environ.pop(applog.ENV_DEBUG, None)
        else:
            applog.os.environ[applog.ENV_DEBUG] = self._env

    def test_verbose_forces_debug(self):
        self.assertTrue(applog.debug_enabled(verbose=True))

    def test_env_debug_on(self):
        applog.os.environ[applog.ENV_DEBUG] = "1"
        self.assertTrue(applog.debug_enabled())

    def test_env_debug_zero_is_off(self):
        applog.os.environ[applog.ENV_DEBUG] = "0"
        self.assertFalse(applog.debug_enabled())

    def test_default_is_info(self):
        applog.os.environ.pop(applog.ENV_DEBUG, None)
        self.assertFalse(applog.debug_enabled())


class Dir(unittest.TestCase):
    def test_env_dir_overrides(self):
        import os
        old = os.environ.get(applog.ENV_DIR)
        os.environ[applog.ENV_DIR] = r"D:\tmp\batbar-ut"
        try:
            self.assertEqual(applog.default_dir(), Path(r"D:\tmp\batbar-ut"))
        finally:
            if old is None:
                os.environ.pop(applog.ENV_DIR, None)
            else:
                os.environ[applog.ENV_DIR] = old

    def test_default_under_localappdata(self):
        import os
        old = os.environ.get(applog.ENV_DIR)
        os.environ.pop(applog.ENV_DIR, None)
        try:
            d = applog.default_dir()
        finally:
            if old is not None:
                os.environ[applog.ENV_DIR] = old
        self.assertEqual(d.name, "logs")
        self.assertEqual(d.parent.name, applog.APP_DIR)
