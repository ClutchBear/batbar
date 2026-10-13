# -*- coding: utf-8 -*-
"""「幽灵鼠标条目」防护（2026-10-13）。

用户报：**没有任何设备连接**时启动托盘，图标先显示 `?`、悬停提示
「鼠标 罗技 G304：未知」，几秒后才变 `--`。

根因：`_last_info` 允许持有 **percent=None** 的 BatteryInfo（设备在但报不出
电量，例如接收器在、鼠标关着，或刚上电那一瞬间）。于是
`devices_snapshot()` 造出一条 `{"鼠标 罗技 G304": None}` 的**幽灵条目**：

  * 菜单多一行「鼠标 罗技 G304 ?%」；
  * `_icon_target()` 选不出有效目标 → `_apply_icon` 落到「有条目但全读不出」
    分支 → 画 `?` + tooltip 用跟踪源名。

修法三条（本文件锁死）：
  1. `devices_snapshot()` 只收 percent 非 None 的鼠标读数；
  2. `_tracked_read()` 不把 percent=None 的 info 写进 `_last_info`；
  3. `_on_notify()` 同理（但消息照常转发）。
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from batbar import agent as A


def _sess_returning(info):
    class Sess:
        def read_battery(self):
            return info
    return Sess()


class SnapshotSkipsNullMouse(unittest.TestCase):
    def _agent(self, info):
        a = object.__new__(A.Agent)
        a._last_info = info
        a._tracked = "hidpp"
        a._bt_battery = lambda: mock.Mock(cached_devices=lambda: {})
        return a

    def test_null_percent_not_in_snapshot(self):
        """`_last_info.percent is None` → 快照里不许出现鼠标条目。"""
        a = self._agent(types.SimpleNamespace(percent=None, charging=False))
        self.assertNotIn("鼠标 罗技 G304", a.devices_snapshot())

    def test_real_percent_still_in_snapshot(self):
        a = self._agent(types.SimpleNamespace(percent=90, charging=False))
        self.assertEqual(a.devices_snapshot().get("鼠标 罗技 G304"), 90)

    def test_no_last_info_no_entry(self):
        a = self._agent(None)
        self.assertNotIn("鼠标 罗技 G304", a.devices_snapshot())


class TrackedReadRejectsNullPercent(unittest.TestCase):
    def _agent(self):
        a = object.__new__(A.Agent)
        a._tracked = "hidpp"
        a._last_info = None
        a._last_read_ts = 0.0
        a._sleep_until = 0.0
        a._session = None
        a._lock = __import__("threading").Lock()
        return a

    def test_null_percent_info_not_cached(self):
        a = self._agent()
        a._ensure_device = lambda: _sess_returning(
            types.SimpleNamespace(percent=None, charging=False))
        pct, chg, st = A.Agent._tracked_read(a)
        self.assertIsNone(pct)
        self.assertEqual(st, "sleeping")
        self.assertIsNone(a._last_info, "percent=None 的 info 不许进 _last_info")

    def test_real_reading_cached(self):
        a = self._agent()
        a._ensure_device = lambda: _sess_returning(
            types.SimpleNamespace(percent=77, charging=True))
        pct, chg, st = A.Agent._tracked_read(a)
        self.assertEqual((pct, chg, st), (77, True, "ok"))
        self.assertEqual(a._last_info.percent, 77)


class NotifyRejectsNullPercent(unittest.TestCase):
    def _agent(self):
        a = object.__new__(A.Agent)
        a._last_info = None
        a._last_read_ts = 0.0
        a._broadcast = lambda _d: None
        return a

    def test_null_percent_notify_not_cached(self):
        a = self._agent()
        A.Agent._on_notify(a, types.SimpleNamespace(
            percent=None, charging=False, source="0x1000"))
        self.assertIsNone(a._last_info)

    def test_real_notify_cached(self):
        a = self._agent()
        A.Agent._on_notify(a, types.SimpleNamespace(
            percent=55, charging=False, source="0x1000"))
        self.assertEqual(a._last_info.percent, 55)


if __name__ == "__main__":
    unittest.main()
