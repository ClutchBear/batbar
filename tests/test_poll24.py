"""poll24 轮询器 + dongle 命令应答帧解析的防回归测试。

样本帧全部来自 2026-10-12 16:56 监听窗口实抓（probes/snap-listen2.log）。
"""

import os
import time
import unittest
from unittest import mock

from batbar import dongle, poll24

# 实抓：getBattery 命令应答帧（93%）——sum8 含首字节 0x13：
# 13+4a+01+00+02+5d+01 = 0xBE ✓
RESP_93 = bytes.fromhex("13 4a 01 00 02 5d 01 00 00 00 00 00 00 00 00 00 00 00 00 be".replace(" ", ""))

# 实抓：通知电量帧（95%）——既有 0a 帧形，回归保护
NOTIFY_95 = bytes.fromhex("13 0a 01 00 04 05 5f 01 00 00 00 00 00 00 00 00 00 00 00 87".replace(" ", ""))

# 实抓：Hub 其它命令的应答（opcode 0x05/0x41）——必须忽略
OTHER_05 = bytes.fromhex("13 05 01 00 0a 03 00 00 00 01 35 01 00 00 00 00 00 00 00 5d".replace(" ", ""))
OTHER_41 = bytes.fromhex("13 41 24 00 0e 00 00 00 29 00 00 00 35 00 00 00 2b 00 00 0f".replace(" ", ""))


class QueryFrame(unittest.TestCase):
    def test_layout(self):
        f = poll24.query_frame()
        self.assertEqual(len(f), 20)  # = OutputReportByteLength
        self.assertEqual(f[0], 0x13)  # reportID / magic
        self.assertEqual(f[1], 0x4A)  # getBattery opcode
        self.assertEqual(f[2], 0x01)
        self.assertEqual(f[-1], 0x5E)  # sum8: 13+4a+01
        self.assertEqual(sum(f[:-1]) & 0xFF, f[-1])

    def test_matches_constant(self):
        # QUERY_FRAME 与 query_frame() 必须一致（有人改一处漏另一处会立刻炸）
        self.assertEqual(poll24.QUERY_FRAME, poll24.query_frame())


class ParseQueryResponse(unittest.TestCase):
    def test_real_frame_93(self):
        self.assertEqual(dongle.parse_frame(RESP_93), ("battery", 93))

    def test_corrupted_rejected(self):
        bad = bytearray(RESP_93)
        bad[5] = 0x60  # 改电量不改校验
        self.assertIsNone(dongle.parse_frame(bytes(bad)))

    def test_other_opcodes_ignored(self):
        # Hub 会同时轮询别的命令（0x05 状态、0x41 性能），都不是电量
        self.assertIsNone(dongle.parse_frame(OTHER_05))
        self.assertIsNone(dongle.parse_frame(OTHER_41))

    def test_notify_battery_still_works(self):
        # 既有 0a 通知帧形不能被新分支破坏
        self.assertEqual(dongle.parse_frame(NOTIFY_95), ("battery", 95))

    def test_pct_range_guard(self):
        bad = bytearray(RESP_93)
        bad[5] = 0xFF  # 越界百分比
        bad[-1] = sum(bad[:8]) & 0xFF  # 校验补对，只剩越界
        self.assertIsNone(dongle.parse_frame(bytes(bad)))


class PollOnce(unittest.TestCase):
    def test_skip_when_no_path(self):
        with mock.patch.object(dongle, "seen_path", return_value=None):
            self.assertFalse(poll24.poll_once("不存在"))

    def test_skip_when_open_fails(self):
        # 假路径 → CreateFileW 失败 → False 且不抛
        with mock.patch.object(dongle, "seen_path", return_value=r"\\?\\nope"):
            self.assertFalse(poll24.poll_once("假路径"))


class Interval(unittest.TestCase):
    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BATBAR_SCAN_24G", None)
            self.assertEqual(poll24.scan_interval(), 60.0)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"BATBAR_SCAN_24G": "30"}):
            self.assertEqual(poll24.scan_interval(), 30.0)

    def test_env_invalid_falls_back(self):
        with mock.patch.dict(os.environ, {"BATBAR_SCAN_24G": "abc"}):
            self.assertEqual(poll24.scan_interval(), 60.0)


class Scan(unittest.TestCase):
    """扫描制核心判据：frame_ts 在本轮窗口内前进 = 在线。

    桩掉 poll_once（不碰真硬件）与 SETTLE（不真等 1.5s），用手工塞
    _stats 模拟「应答帧入库」的时序。
    """

    NAME = "键盘 迈从 K87"

    def setUp(self):
        dongle._stats.clear()
        poll24.reset()
        self._old_settle = poll24.SETTLE
        poll24.SETTLE = 0.01

    def tearDown(self):
        dongle._stats.clear()
        poll24.reset()
        poll24.SETTLE = self._old_settle

    def _run_scan(self, respond: bool):
        """respond=True 模拟设备在窗口内回帧（frame_ts 前进）。"""

        def fake_poll(name):
            if respond:
                dongle._stats.setdefault(name, dongle._new_stat())
                dongle._stats[name]["frame_ts"] = time.time()
            return True

        with mock.patch.object(poll24, "poll_once", fake_poll):
            return poll24.scan([self.NAME])

    def test_response_means_online(self):
        dongle._stats[self.NAME] = dongle._new_stat()
        result = self._run_scan(respond=True)
        self.assertEqual(result, {self.NAME: True})
        self.assertTrue(poll24.online(self.NAME))
        self.assertFalse(dongle.is_offline(self.NAME))

    def test_silence_means_offline(self):
        # 有历史记录（旧 frame_ts）但本轮不应答 → 不在（正是用户报的
        # 「K87 完全关机还显示 2.4G」场景：旧帧不能当活证据）
        now = time.time()
        dongle._stats[self.NAME] = {
            "battery": 90,
            "battery_ts": now - 999,
            "frame_ts": now - 999,
            "sleeping": False,
            "sleep_ts": None,
        }
        result = self._run_scan(respond=False)
        self.assertEqual(result, {self.NAME: False})
        self.assertTrue(dongle.is_offline(self.NAME))

    def test_unknown_name_gets_offline_conclusion(self):
        result = self._run_scan(respond=False)
        self.assertEqual(result, {self.NAME: False})

    def test_scanned_flag_after_run(self):
        self.assertFalse(poll24.scanned(self.NAME))
        self._run_scan(respond=False)
        self.assertTrue(poll24.scanned(self.NAME))


class WindowOverlap(unittest.TestCase):
    """等待窗口必须**所有设备共享一个**（2026-10-16 改）。

    旧写法是「发 A → 等 1.5s → 发 B → 等 1.5s …」，一轮 1.5s × 台数；耗时全在
    等应答上，发帧本身毫秒级，重叠起来毫无副作用 —— 前提见 `scan()` 注释：
    每台**发帧之前**先取自己的基准 ts，别台的帧污染不到本台的结论。

    这两条钉住两件事：等待次数与台数无关；重叠后判据仍然是"每台各自"的。
    """

    NAMES = ("键盘 迈从 K87", "鼠标 迈从 A5", "键盘 LEOBOG Hi98C Pro")

    def setUp(self):
        dongle._stats.clear()
        poll24.reset()
        self._old_settle = poll24.SETTLE
        poll24.SETTLE = 0.01

    def tearDown(self):
        dongle._stats.clear()
        poll24.reset()
        poll24.SETTLE = self._old_settle

    def test_one_wait_window_regardless_of_device_count(self):
        waits = []
        with (
            mock.patch.object(poll24, "poll_once", lambda n: True),
            mock.patch.object(poll24.time, "sleep", lambda s: waits.append(s)),
        ):
            poll24.scan(self.NAMES)
        self.assertEqual([poll24.SETTLE], waits, f"等待窗口退化成每台一个了：{waits}")

    def test_per_device_verdict_still_independent(self):
        """只有**自己**回了帧的那台判在线，别台的帧不能把它带成在线。"""
        responding = {self.NAMES[1]}  # 只让第二台应答

        def fake_poll(name):
            if name in responding:
                dongle._stats.setdefault(name, dongle._new_stat())
                dongle._stats[name]["frame_ts"] = time.time()
            return True

        with mock.patch.object(poll24, "poll_once", fake_poll):
            out = poll24.scan(self.NAMES)

        self.assertEqual({self.NAMES[0]: False, self.NAMES[1]: True, self.NAMES[2]: False}, out)


class SkipWhenGate(unittest.TestCase):
    """全屏/游戏时跳过扫描的判据注入（2026-10-13）。"""

    def tearDown(self):
        poll24.set_skip_when(None)

    def test_default_no_skip(self):
        poll24.set_skip_when(None)
        self.assertFalse(poll24._should_skip())

    def test_predicate_true_skips(self):
        poll24.set_skip_when(lambda: True)
        self.assertTrue(poll24._should_skip())

    def test_predicate_false_does_not_skip(self):
        poll24.set_skip_when(lambda: False)
        self.assertFalse(poll24._should_skip())

    def test_predicate_exception_treated_as_no_skip(self):
        """★ 判据自己炸了 → 按"不跳过"处理。宁可多扫一轮，也别永远不扫。"""

        def boom():
            raise RuntimeError("gate down")

        poll24.set_skip_when(boom)
        self.assertFalse(poll24._should_skip())

    def test_can_clear_predicate(self):
        poll24.set_skip_when(lambda: True)
        self.assertTrue(poll24._should_skip())
        poll24.set_skip_when(None)
        self.assertFalse(poll24._should_skip())


if __name__ == "__main__":
    unittest.main()
