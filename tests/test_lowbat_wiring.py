"""agent 低电量接线：_feed_lowbat 用剥后缀基名做 key，_feed_snapshot 覆盖全设备。

只测接线本身（读数如何流进状态机），不测状态机判定（见 test_lowbattery）。
"""

from __future__ import annotations

import unittest

from batbar import agent as A
from batbar import lowbattery as LB
from batbar import receiver24 as R24


class _RecordingWatcher:
    def __init__(self):
        self.feeds: list[tuple] = []

    def feed(self, name, pct, charging=False, wired=False):
        self.feeds.append((name, pct, charging, wired))
        return False


class FeedWiring(unittest.TestCase):
    def setUp(self):
        LB.reset()
        self.rec = _RecordingWatcher()
        self._saved_get = LB.get
        LB.get = lambda *a, **k: self.rec  # 注入假状态机
        # 桩掉真实 USB 枚举：_feed_lowbat 会调 wired_bases()，测试机不能真起 shell
        self._saved_wired = R24.wired_bases
        R24.wired_bases = lambda *a, **k: set()

    def tearDown(self):
        LB.get = self._saved_get
        R24.wired_bases = self._saved_wired
        LB.reset()

    def _agent(self):
        ag = A.Agent.__new__(A.Agent)  # 绕过 __init__ 的托盘/主题副作用
        ag._lowbat = self.rec
        ag._last_info = None
        ag._tracked = "hidpp"
        return ag

    def test_feed_uses_base_name_key(self):
        """ "LEOBOG 键盘 (2.4G)" 喂进去要剥成基名，保证跨连接方式计数连续。"""
        ag = self._agent()
        ag._feed_lowbat("LEOBOG 键盘 (2.4G)", 8)
        self.assertEqual(self.rec.feeds[0][0], "LEOBOG 键盘")

    def test_feed_passes_charging(self):
        ag = self._agent()
        ag._feed_lowbat("罗技鼠标", 12, charging=True)
        _n, pct, chg, _w = self.rec.feeds[0]
        self.assertEqual(pct, 12)
        self.assertTrue(chg)


class FeedSnapshot(unittest.TestCase):
    def setUp(self):
        LB.reset()
        self.rec = _RecordingWatcher()
        self._saved_get = LB.get
        LB.get = lambda *a, **k: self.rec
        self._saved_wired = R24.wired_bases
        R24.wired_bases = lambda *a, **k: set()

    def tearDown(self):
        LB.get = self._saved_get
        R24.wired_bases = self._saved_wired
        LB.reset()

    def test_snapshot_feeds_every_device(self):
        """快照里每台设备都要被喂一遍（提醒不限跟踪源）。"""
        ag = A.Agent.__new__(A.Agent)
        ag._lowbat = self.rec
        ag._last_info = None
        ag._tracked = "hidpp"
        ag.devices_snapshot = lambda: {"罗技鼠标": 5, "西圣耳机": 90, "LEOBOG 键盘 (2.4G)": 3}
        ag._feed_snapshot()
        fed = {f[0] for f in self.rec.feeds}
        self.assertIn("罗技鼠标", fed)
        self.assertIn("西圣耳机", fed)
        self.assertIn("LEOBOG 键盘", fed)  # 基名


if __name__ == "__main__":
    unittest.main()
