"""罗技条目在/离线探测（2026-10-13 判据定稿：两次不应答 = 不在）。

鼠标关机或接收器拔掉时，`_last_info` 不再永久挂在快照里：
`_probe_mouse` 读数过期就探一次，两次都无应答（内置一次复读挡单次毛刺）
→ 清掉条目 + 30s 退避；0xFF 回落会话额外丢会话重扫。
（历史：01:52 曾把「真槽位不应答」判成睡眠保留，11:00 被实测证伪——
静置 40s 的 G304 依然应答，判据说明见 `agent._read_with_second_chance`。）
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from batbar import agent as A
from batbar import hidpp as H
from batbar.session import TransportClosed


def _fake_agent(last_info=None, age=0.0):
    """造一个只带探测所需字段的 Agent（不跑 __init__，不碰硬件）。"""
    a = object.__new__(A.Agent)
    a._last_info = last_info
    a._last_read_ts = time.monotonic() - age
    a._sleep_until = 0.0
    a._session = object() if last_info else None
    a._drop_calls = 0
    a._drop_device = lambda: setattr(a, "_drop_calls", a._drop_calls + 1)
    return a


_INFO = H.BatteryInfo(90, None, False, None, "hidpp")


class SecondChance(unittest.TestCase):
    """`_read_with_second_chance`：一次复读挡单次毛刺，两次都哑 = 不在。"""

    def _sess(self, answers):
        sess = mock.Mock()
        sess._devnum = 1
        sess.read_battery.side_effect = answers
        return sess

    def test_first_answer_used(self):
        """第一枪就应答 → 直接用，不再戳第二枪。"""
        new = H.BatteryInfo(88, None, False, None, "hidpp")
        sess = self._sess([new, mock.Mock()])
        self.assertIs(A.Agent._read_with_second_chance(object.__new__(A.Agent), sess), new)
        self.assertEqual(sess.read_battery.call_count, 1)

    def test_second_answer_rescued(self):
        """第一枪哑、第二枪应答 = 单次毛刺 → 返回读数（条目保留）。"""
        new = H.BatteryInfo(88, None, False, None, "hidpp")
        sess = self._sess([None, new])
        self.assertIs(A.Agent._read_with_second_chance(object.__new__(A.Agent), sess), new)
        self.assertEqual(sess.read_battery.call_count, 2)

    def test_both_none_is_absent(self):
        """两枪都哑 = 设备不在（关机/离队）→ None。"""
        sess = self._sess([None, None])
        self.assertIsNone(A.Agent._read_with_second_chance(object.__new__(A.Agent), sess))
        self.assertEqual(sess.read_battery.call_count, 2)


class ProbeMouse(unittest.TestCase):
    def test_fresh_reading_skips_probe(self):
        """读数还新鲜（≤MOUSE_FRESH）→ 根本不戳硬件。"""
        a = _fake_agent(_INFO, age=1.0)
        a._ensure_device = lambda: (_ for _ in ()).throw(AssertionError("新鲜期不该探测"))
        a._probe_mouse()
        self.assertEqual(a._last_info, _INFO)

    def test_stale_no_answer_hides_on_real_slot(self):
        """过期 + 真槽位**两次**不应答 = 鼠标关机/离队 → 条目隐藏（只退避，
        会话保留——下一轮若设备回来原会话还能用）。

        2026-10-13 11:00 定稿：旧实现把真槽位不应答判成「睡眠保留」，结果
        关机后点「立即刷新」鼠标还在（用户报）。实测静置 40s 的 G304 照样
        应答 ⇒ 不应答 ≠ 睡眠。单次毛刺由复读挡住，这里测的是两次都哑。
        """
        a = _fake_agent(_INFO, age=A.MOUSE_FRESH + 5)
        sess = mock.Mock()
        sess._devnum = 1
        sess.read_battery.return_value = None
        a._ensure_device = lambda: sess
        a._probe_mouse()
        self.assertIsNone(a._last_info)
        self.assertEqual(a._drop_calls, 0)
        self.assertGreater(a._sleep_until, time.monotonic() + 25)

    def test_single_glitch_rescued_in_probe(self):
        """真槽位第一枪哑、第二枪应答 = 毛刺 → 条目保留并刷新读数。"""
        a = _fake_agent(_INFO, age=A.MOUSE_FRESH + 5)
        new = H.BatteryInfo(88, None, False, None, "hidpp")
        sess = mock.Mock()
        sess._devnum = 1
        sess.read_battery.side_effect = [None, new]
        a._ensure_device = lambda: sess
        a._probe_mouse()
        self.assertIs(a._last_info, new)
        self.assertEqual(a._sleep_until, 0.0)

    def test_stale_0xff_no_answer_hides_entry(self):
        """0xFF 回落会话（扫不到对码设备）→ 设备真不在 → 条目清掉 + 丢会话。"""
        a = _fake_agent(_INFO, age=A.MOUSE_FRESH + 5)
        sess = mock.Mock()
        sess._devnum = H.DEV_RECEIVER
        sess.read_battery.return_value = None
        a._ensure_device = lambda: sess
        a._probe_mouse()
        self.assertIsNone(a._last_info)
        self.assertEqual(a._drop_calls, 1)

    def test_stale_answer_refreshes(self):
        """过期但设备应答 → 刷新缓存，条目保留，退避清零。"""
        a = _fake_agent(_INFO, age=A.MOUSE_FRESH + 5)
        new = H.BatteryInfo(88, None, False, None, "hidpp")
        sess = mock.Mock()
        sess.read_battery.return_value = new
        a._ensure_device = lambda: sess
        a._probe_mouse()
        self.assertIs(a._last_info, new)
        self.assertEqual(a._sleep_until, 0.0)

    def test_receiver_unplugged_enumeration_none(self):
        """枚举不到设备（接收器拔掉的典型路径）→ 清条目 + 退避。"""
        a = _fake_agent(_INFO, age=A.MOUSE_FRESH + 5)
        a._ensure_device = lambda: None
        a._probe_mouse()
        self.assertIsNone(a._last_info)
        self.assertGreater(a._sleep_until, time.monotonic() + 25)

    def test_transport_closed_drops_session(self):
        """句柄失效（TransportClosed）→ 丢会话 + 清条目，下轮重枚举自愈。"""
        a = _fake_agent(_INFO, age=A.MOUSE_FRESH + 5)

        def boom():
            raise TransportClosed("write error")

        a._ensure_device = boom
        a._probe_mouse()
        self.assertIsNone(a._last_info)
        self.assertEqual(a._drop_calls, 1)

    def test_ff_fallback_session_dropped_on_no_answer(self):
        """★ 复活关键路径：0xFF 回落会话（建会话时鼠标在睡、槽位扫空）读
        不出电池 —— 不应答时必须**丢会话重扫**，否则冻死在接收器本体上，
        插上后刷新多少次都不回来（2026-10-12 日志：device_index=255 + 无限
        "no known battery feature"）。"""
        a = _fake_agent(_INFO, age=A.MOUSE_FRESH + 5)
        sess = mock.Mock()
        sess._devnum = H.DEV_RECEIVER  # 0xFF 死会话
        sess.read_battery.return_value = None
        a._ensure_device = lambda: sess
        a._probe_mouse()
        self.assertIsNone(a._last_info)
        self.assertEqual(a._drop_calls, 1)  # 会话被丢，下轮重扫槽位

    def test_real_slot_no_answer_keeps_session(self):
        """真槽位会话两次不应答：条目隐藏但**会话保留**（不重建会话）——
        设备重新开机后下一轮探测直接用原会话复活。"""
        a = _fake_agent(_INFO, age=A.MOUSE_FRESH + 5)
        sess = mock.Mock()
        sess._devnum = 1
        sess.read_battery.return_value = None
        a._ensure_device = lambda: sess
        a._probe_mouse()
        self.assertIsNone(a._last_info)
        self.assertEqual(a._drop_calls, 0)

    def test_backoff_suppresses_reprobe(self):
        """退避期内再调 → 不重复戳硬件（防 9s 循环狂戳关机的鼠标）。"""
        a = _fake_agent(_INFO, age=A.MOUSE_FRESH + 5)
        a._sleep_until = time.monotonic() + 20
        a._ensure_device = lambda: (_ for _ in ()).throw(AssertionError("退避期不该探测"))
        a._probe_mouse()  # 不炸即通过
        self.assertEqual(a._last_info, _INFO)

    def test_no_device_still_probes_periodically(self):
        """本机从没读到过罗技（_last_info=None、无会话）也要周期探：
        鼠标后开机/后插接收器要能自己回来。"""
        a = _fake_agent(None, age=0.0)
        new = H.BatteryInfo(95, None, False, None, "hidpp")
        sess = mock.Mock()
        sess.read_battery.return_value = new
        a._ensure_device = lambda: sess
        a._probe_mouse()
        self.assertIs(a._last_info, new)


class DiscoverDevnum(unittest.TestCase):
    """槽位发现（2026-10-12 修「插上后永不复活」）：记住上次槽位走快路径；
    每槽有唤醒预算（timeout/retries 由 _hit 传入），全空才回落 0xFF。"""

    def _agent_and_probe(self, hits, remembered):
        """hits = 会应答 battery_feature 的槽位集合；返回 (agent, 调用记录)。"""
        a = object.__new__(A.Agent)
        a._last_devnum = remembered
        calls = []

        class FakeProbe:
            def __init__(self, *args, **kw):
                self._devnum = None
                self._feat_cache = {}
                self._battery_feat = None
                self._timeout = None
                self._retries = None

            def battery_feature(self):
                calls.append((self._devnum, self._timeout, self._retries))
                return (0x1004, 1) if self._devnum in hits else None

        tr = mock.Mock()
        tr._frame_len = 7
        with (
            mock.patch.object(A.T, "HidapiTransport", lambda *a2, **k2: tr),
            mock.patch.object(A, "HidppSession", FakeProbe),
        ):
            idx, flen = a._discover_devnum(mock.Mock(), [], 7, None)
        return idx, calls

    def test_remembered_slot_fast_path(self):
        """记住槽位 1 且应答 → 只戳一枪就返回，不再全扫。"""
        idx, calls = self._agent_and_probe({1}, remembered=1)
        self.assertEqual(idx, 1)
        self.assertEqual(calls, [(1, 0.8, 1)])

    def test_remembered_miss_falls_to_full_scan(self):
        """记住的槽位不应答（重新对码）→ 全扫命中槽 2 并更新记忆。"""
        idx, calls = self._agent_and_probe({2}, remembered=1)
        self.assertEqual(idx, 2)
        self.assertEqual(calls[0][0], 1)  # 先试记住的
        self.assertEqual(calls[-1][0], 2)  # 全扫命中

    def test_full_scan_updates_memory(self):
        a2 = object.__new__(A.Agent)
        a2._last_devnum = None
        tr = mock.Mock()
        tr._frame_len = 7

        class FakeProbe:
            def __init__(self, *args, **kw):
                self._devnum = None
                self._feat_cache = {}
                self._battery_feat = None

            def battery_feature(self):
                return (0x1004, 1) if self._devnum == 3 else None

        with (
            mock.patch.object(A.T, "HidapiTransport", lambda *a2_, **k2: tr),
            mock.patch.object(A, "HidppSession", FakeProbe),
        ):
            idx, _ = a2._discover_devnum(mock.Mock(), [], 7, None)
        self.assertEqual(idx, 3)
        self.assertEqual(a2._last_devnum, 3)

    def test_all_empty_falls_back_to_receiver(self):
        """全不应答（鼠标关机）→ 回落 0xFF，由探测层的「死会话丢弃」兜住。"""
        idx, calls = self._agent_and_probe(set(), remembered=None)
        self.assertEqual(idx, H.DEV_RECEIVER)
        self.assertEqual(len(calls), 7)  # 1-7 各试一遍


class ManualRefreshConsistency(unittest.TestCase):
    """「立即刷新」与后台探测同口径（2026-10-13 11:00 用户报的 bug 11 主场）：
    鼠标关机后点刷新，条目必须当场消失，不能冻在旧读数上。"""

    def test_no_answer_hides_on_real_slot(self):
        """★ bug 11 回归：真槽位不应答（鼠标关机）→ 条目隐藏。
        旧实现判「睡眠保留」，关机后点刷新鼠标还在、还能勾选。"""
        a = _fake_agent(_INFO, age=1.0)
        sess = mock.Mock()
        sess._devnum = 1
        sess.read_battery.return_value = None
        a._ensure_device = lambda: sess
        a._push_fresh_read()
        self.assertIsNone(a._last_info)

    def test_no_answer_clears_on_0xff(self):
        """「立即刷新」遇 0xFF 回落 = 设备真不在 → 清条目。"""
        a = _fake_agent(_INFO, age=1.0)
        sess = mock.Mock()
        sess._devnum = H.DEV_RECEIVER
        sess.read_battery.return_value = None
        a._ensure_device = lambda: sess
        a._push_fresh_read()
        self.assertIsNone(a._last_info)

    def test_enumeration_none_clears(self):
        a = _fake_agent(_INFO, age=1.0)
        a._ensure_device = lambda: None
        a._push_fresh_read()
        self.assertIsNone(a._last_info)

    def test_transport_closed_clears(self):
        a = _fake_agent(_INFO, age=1.0)
        a._ensure_device = lambda: (_ for _ in ()).throw(TransportClosed("gone"))
        a._push_fresh_read()
        self.assertIsNone(a._last_info)
        self.assertEqual(a._drop_calls, 1)


class ManualRefreshRevives(unittest.TestCase):
    """★ 复活（2026-10-12 实测「插回接收器后刷新多次都不出现」）。

    光 `_drop_device()` 只是把门打开——必须**当场重建会话再读一次**，
    否则用户点十次刷新也只是丢了十个死会话，条目照样不回来。
    """

    def _agent(self, devnums, answers):
        """devnums/answers = 每次 _ensure_device 依次产出的 (devnum, 读数)。"""
        a = _fake_agent(_INFO, age=1.0)
        a._sleep_until = time.monotonic() + 30  # 卡在退避里也要能复活
        made = []

        def ensure():
            if not devnums:
                return None
            s = mock.Mock()
            s._devnum = devnums.pop(0)
            s.read_battery.return_value = answers.pop(0)
            made.append(s)
            return s

        a._ensure_device = ensure
        return a, made

    def test_ff_dropped_then_rebuilt_revives(self):
        """0xFF 死会话读不出 → 丢掉 → 重建扫中槽位 1 → 条目当场复活。"""
        new = H.BatteryInfo(90, None, False, None, "hidpp")
        a, made = self._agent([H.DEV_RECEIVER, 1], [None, new])
        a._push_fresh_read()
        self.assertIs(a._last_info, new)  # 当场复活，不用等下一轮
        self.assertEqual(a._drop_calls, 1)  # 死会话确实被丢
        self.assertEqual(len(made), 2)  # 丢了之后确实重建了
        self.assertEqual(a._sleep_until, 0.0)  # 退避解禁，后台也能跟

    def test_ff_dropped_rebuild_still_no_answer_hides(self):
        """重建后依然不应答（鼠标真关机）→ 条目保持隐藏，不冻在旧读数。"""
        a, made = self._agent([H.DEV_RECEIVER, 1], [None, None])
        a._push_fresh_read()
        self.assertIsNone(a._last_info)
        self.assertEqual(a._drop_calls, 1)
        self.assertEqual(len(made), 2)

    def test_ff_dropped_rebuild_enumeration_none(self):
        """丢会话后重新枚举不到（接收器又拔了）→ 条目隐藏，不炸。"""
        a, made = self._agent([H.DEV_RECEIVER], [None])
        a._push_fresh_read()
        self.assertIsNone(a._last_info)
        self.assertEqual(a._drop_calls, 1)

    def test_real_slot_no_answer_hides(self):
        """真槽位会话不应答（鼠标关机）→ 条目隐藏，会话保留不重建。"""
        a, made = self._agent([1], [None])
        a._push_fresh_read()
        self.assertIsNone(a._last_info)
        self.assertEqual(a._drop_calls, 0)
        self.assertEqual(len(made), 1)


if __name__ == "__main__":
    unittest.main()
