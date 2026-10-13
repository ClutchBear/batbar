# -*- coding: utf-8 -*-
"""托盘图标选源规则（2026-10-12 用户拍板，四段式）。

  ① 手动选择（菜单点了某台）→ 图标显示它并**粘住**，直到点另一台或它从
     快照消失（设 BATBAR_MANUAL_TTL 才有 N 秒窗口自动交还）；
  ② 低电长显：有设备 ≤ 阈值 → 显示其中最低的那台并变红；
  ③ 都没有 → 优先显示电量最低的耳机；
  ④ 连耳机都没有 → 快照里任意有读数设备中最低的一台（有线/充电不排除；
     全都读不出才 None）。

Agent 实例用 `object.__new__` 造（不跑 __init__：那会去碰硬件/注册主题监听），
只打桩 devices_snapshot / _tracked_name。
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from batbar import agent as A
from batbar import lowbattery as LB
from batbar import receiver24 as R


def _fake_agent(snap, tracked_name="鼠标 罗技 G304", manual=None, until=0.0):
    a = object.__new__(A.Agent)
    a.devices_snapshot = lambda: dict(snap)
    a._tracked_name = lambda: tracked_name
    a._last_info = None
    a._manual_name = manual
    a._manual_until = until
    return a


class IconTarget(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(R, "wired_bases", lambda *a, **k: set())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_low_device_wins_and_goes_red(self):
        """② 低电长显：50/100/19 → 显示 19 且变红（阈值 20）。"""
        a = _fake_agent({"耳机 西圣 XISEM-Air": 50, "耳机 QCY Era 7": 100,
                         "键盘 迈从 K87 (2.4G)": 19})
        self.assertEqual(a._icon_target(), ("键盘 迈从 K87 (2.4G)", 19, True))

    def test_lowest_of_several_low(self):
        """多台低电取最低。"""
        a = _fake_agent({"耳机 A": 18, "键盘 B": 5})
        self.assertEqual(a._icon_target(), ("键盘 B", 5, True))

    def test_no_low_device_prefers_headphone(self):
        """③ 无低电设备 → 优先显示电量最低的**耳机**（不看阈值）。"""
        a = _fake_agent({"耳机 QCY Era 7": 100, "耳机 西圣 XISEM-Air": 50,
                         "鼠标 罗技 G304": 30})
        self.assertEqual(a._icon_target(), ("耳机 西圣 XISEM-Air", 50, False))

    def test_no_headphone_falls_to_lowest_device(self):
        """④ 终极兜底（2026-10-12 修「只插键盘托盘问号」）：无低电、无耳机 →
        显示快照里**任意有读数设备**中最低的一台（有线/充电不再排除）。"""
        a = _fake_agent({"鼠标 罗技 G304": 90, "键盘 迈从 K87 (2.4G)": 80})
        self.assertEqual(a._icon_target(), ("键盘 迈从 K87 (2.4G)", 80, False))

    def test_wired_only_keyboard_shown(self):
        """用户实测场景：只插键盘（有线 100%）、接收器没插 → 图标显示键盘，
        不再回落成跟踪源的问号。"""
        with mock.patch.object(R, "wired_bases",
                               lambda *a, **k: {"键盘 LEOBOG Hi98C Pro"}):
            a = _fake_agent({"键盘 LEOBOG Hi98C Pro (有线)": 100})
            self.assertEqual(a._icon_target(),
                             ("键盘 LEOBOG Hi98C Pro (有线)", 100, False))

    def test_low_beats_headphone_preference(self):
        """低电长显优先于耳机偏好：键盘 19% 比耳机 50% 更该显示。"""
        a = _fake_agent({"耳机 西圣 XISEM-Air": 50, "键盘 迈从 K87 (2.4G)": 19})
        self.assertEqual(a._icon_target(), ("键盘 迈从 K87 (2.4G)", 19, True))

    def test_threshold_boundary(self):
        """阈值边界：20 不算低（「低于 20」），19 算。21 无低电无耳机 →
        走 ④ 兜底显示它本体（不变红）。"""
        self.assertEqual(_fake_agent({"X": 21})._icon_target(), ("X", 21, False))
        self.assertEqual(_fake_agent({"X": 20})._icon_target(), ("X", 20, True))

    def test_manual_overrides_low(self):
        """① 手动窗口内：即使有低电设备，也先显示用户点的那台。"""
        a = _fake_agent({"耳机 A": 5, "鼠标 罗技 G304": 90},
                        manual="鼠标 罗技 G304",
                        until=time.monotonic() + 30)
        self.assertEqual(a._icon_target(), ("鼠标 罗技 G304", 90, False))

    def test_sticky_never_expires(self):
        """粘住模式（默认）：`_manual_until=inf`，多久都不回退到低电设备。
        （2026-10-12 修「勾了 LEOBOG 十几秒后图标自己跳回耳机」）"""
        a = _fake_agent({"耳机 A": 5, "鼠标 罗技 G304": 90},
                        manual="鼠标 罗技 G304", until=float("inf"))
        self.assertEqual(a._icon_target(), ("鼠标 罗技 G304", 90, False))

    def test_manual_window_expired_falls_back_to_low(self):
        """显式设了窗口（BATBAR_MANUAL_TTL）且过期 → 回退低电设备。"""
        a = _fake_agent({"耳机 A": 5, "鼠标 罗技 G304": 90},
                        manual="鼠标 罗技 G304",
                        until=time.monotonic() - 1)
        self.assertEqual(a._icon_target(), ("耳机 A", 5, True))

    def test_manual_device_gone_ignored(self):
        """手动那台已从快照消失（拔了/断连）→ 不硬撑，回退自动规则。
        粘住模式下同样成立：靠的是「m in snap」判据，与窗口是否到期无关。"""
        a = _fake_agent({"耳机 A": 5}, manual="鼠标 罗技 G304",
                        until=float("inf"))
        self.assertEqual(a._icon_target(), ("耳机 A", 5, True))

    def test_zero_and_none_excluded(self):
        """0%（2.4G 毛刺）与 None（无读数）都不算低电、也不参与耳机偏好。"""
        a = _fake_agent({"毛刺设备": 0, "未知设备": None})
        self.assertIsNone(a._icon_target())
        # 耳机 A 30% 不低，但无低电设备 → 耳机偏好命中它（非红）
        b = _fake_agent({"毛刺设备": 0, "未知设备": None, "耳机 A": 30})
        self.assertEqual(b._icon_target(), ("耳机 A", 30, False))

    def test_wired_excluded_from_alarm_slots(self):
        """有线设备不参与低电/耳机抢图标（插着电不是「需要关注」），
        但兜底层仍可显示快照里其他有读数的设备。"""
        with mock.patch.object(R, "wired_bases", lambda *a, **k: {"耳机 A"}):
            a = _fake_agent({"耳机 A": 5, "鼠标 罗技 G304": 90})
            self.assertEqual(a._icon_target(), ("鼠标 罗技 G304", 90, False))

    def test_all_zero_or_none_still_none(self):
        """快照里只有 0（毛刺）和 None（无读数）→ 兜底也挑不出 → None。"""
        a = _fake_agent({"毛刺设备": 0, "未知设备": None})
        self.assertIsNone(a._icon_target())


class ManualTtl(unittest.TestCase):
    def test_default_is_sticky(self):
        """默认 MANUAL_TTL=None = 粘住不回退（2026-10-12 用户拍板）。"""
        self.assertIsNone(A.MANUAL_TTL)

    def test_set_tracked_sticky_uses_inf(self):
        """粘住模式下 _set_tracked 把 _manual_until 设成 inf，且真
        _schedule_manual_revert 不排任何定时器线程。"""
        a = object.__new__(A.Agent)
        a._tracked = "24G:键盘 LEOBOG Hi98C Pro"
        a._bt_misses = 0
        a._tracked_name = lambda: "键盘 LEOBOG Hi98C Pro (有线)"
        a._icon = None                       # 不触发刷新线程
        with mock.patch.object(A.threading, "Thread") as th:
            A.Agent._set_tracked(a, a._tracked)
        self.assertEqual(a._manual_until, float("inf"))
        th.assert_not_called()               # 粘住 = 不排回退定时器

    def test_set_tracked_with_window(self):
        """显式设窗口（BATBAR_MANUAL_TTL）→ _manual_until 有限且排回退线程。"""
        a = object.__new__(A.Agent)
        a._tracked = "hidpp"
        a._bt_misses = 0
        a._tracked_name = lambda: "鼠标 罗技 G304"
        a._icon = None
        with mock.patch.object(A, "MANUAL_TTL", 10.0), \
             mock.patch.object(A.threading, "Thread") as th:
            A.Agent._set_tracked(a, "hidpp")
        self.assertLess(a._manual_until, float("inf"))
        th.assert_called_once()              # 窗口模式排了回退定时器

    def test_threshold_default_is_20(self):
        """用户需求「低于 20 的设备长显」。"""
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("BATBAR_LOWBAT_THRESHOLD", None)
            self.assertEqual(LB.threshold(), 20)


class IconApply(unittest.TestCase):
    """图标落地：托盘未建好时不许写缓存（2026-10-12 启动后图标卡 90 的根因）。"""

    def setUp(self):
        self._patch = mock.patch.object(R, "wired_bases", lambda *a, **k: set())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _agent(self, snap):
        a = _fake_agent(snap)
        a._last_icon = None
        a._last_tooltip = ""
        a._last_raw = (None, False)
        a._icon = None
        return a

    def test_no_tray_no_cache(self):
        """托盘未建好 → _apply_icon 返回 False 且**不写缓存**（下次重试）。"""
        a = self._agent({"耳机 A": 50})
        self.assertFalse(a._apply_icon(90, False))
        self.assertIsNone(a._last_icon)

    def test_applied_updates_cache(self):
        """托盘就绪 → 返回 True 并写入缓存（缓存的是选源结果，不是跟踪源）。"""
        class FakeIcon:
            def __init__(self):
                self.icon = None
                self.title = None

        a = self._agent({"耳机 A": 50, "鼠标 罗技 G304": 90})
        a._icon = FakeIcon()
        self.assertTrue(a._apply_icon(90, False))
        self.assertIsNotNone(a._last_icon)
        self.assertEqual(a._last_icon.percent, 50)      # 耳机优先，非跟踪源的 90
        self.assertFalse(a._last_icon.warning)


class MenuCheck(unittest.TestCase):
    """菜单对勾必须与托盘图标显示的那台一致（用户需求 2026-10-12）。"""

    def setUp(self):
        self._patch = mock.patch.object(R, "wired_bases", lambda *a, **k: set())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _agent(self, snap, tracked_name="鼠标 罗技 G304"):
        a = _fake_agent(snap, tracked_name=tracked_name)
        return a

    def test_check_follows_icon(self):
        """图标显示西圣 50 → 对勾该在它身上（不是跟踪源的罗技）。"""
        a = self._agent({"耳机 QCY Era 7": 100, "耳机 西圣 XISEM-Air": 50,
                         "鼠标 罗技 G304": 90})
        self.assertEqual(a._icon_display_name(), "耳机 西圣 XISEM-Air")
        self.assertEqual(a._key_for_name(a._icon_display_name()),
                         "BT:耳机 西圣 XISEM-Air")

    def test_check_follows_low_device(self):
        """低电长显优先：键盘 19% → 对勾在键盘。"""
        a = self._agent({"耳机 西圣 XISEM-Air": 50, "键盘 迈从 K87 (2.4G)": 19})
        self.assertEqual(a._key_for_name(a._icon_display_name()),
                         "24G:键盘 迈从 K87")

    def test_check_falls_to_lowest_device(self):
        """无低电、无耳机 → ④ 兜底显示快照里读数最低的那台（不再是跟踪源），
        对勾跟过去 —— 图标与列表永远同源。"""
        a = self._agent({"鼠标 罗技 G304": 90, "键盘 迈从 K87 (2.4G)": 80})
        self.assertEqual(a._icon_display_name(), "键盘 迈从 K87 (2.4G)")
        self.assertEqual(a._key_for_name("键盘 迈从 K87 (2.4G)"),
                         "24G:键盘 迈从 K87")

    def test_key_for_name_variants(self):
        self.assertEqual(A.Agent._key_for_name(None, "鼠标 罗技 G304"), "hidpp")
        self.assertEqual(A.Agent._key_for_name(None, "键盘 迈从 K87 (2.4G)"),
                         "24G:键盘 迈从 K87")
        self.assertEqual(A.Agent._key_for_name(None, "键盘 LEOBOG Hi98C Pro (有线)"),
                         "24G:键盘 LEOBOG Hi98C Pro")
        self.assertEqual(A.Agent._key_for_name(None, "耳机 QCY Era 7"),
                         "BT:耳机 QCY Era 7")


class SnapshotCoherence(unittest.TestCase):
    """同一次弹菜单只准读一份快照（2026-10-12 修「托盘 91、对勾行 92」）。

    根因：菜单标签用 devices_snapshot() 第一次读取，对勾走
    _icon_display_name()→_icon_target() 又读第二次；2.4G 电量帧是 ±2 抖动的
    ADC，两次读取跨帧 → 对勾那行的数字和图标数字对不上。修法 = 把同一份
    snap 一路传下去。
    """

    def setUp(self):
        self._patch = mock.patch.object(R, "wired_bases", lambda *a, **k: set())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_explicit_snap_skips_second_read(self):
        """传了 snap 就绝不能再碰 devices_snapshot（这里让它一调就炸）。"""
        a = _fake_agent({"耳机 A": 50})
        a.devices_snapshot = lambda: (_ for _ in ()).throw(
            AssertionError("不该二次读快照"))
        self.assertEqual(a._icon_target({"耳机 A": 50}), ("耳机 A", 50, False))
        self.assertEqual(a._icon_display_name({"耳机 A": 50}), "耳机 A")

    def test_jitter_between_reads_not_visible_in_menu(self):
        """模拟两次读取跨帧（第一份 91 第二份 92）：传 snap 后第二次读取
        根本不会发生 —— 标签/对勾/图标同步全用弹菜单那一刻的同一份。"""
        calls = []

        def snap():
            calls.append(1)
            return {"键盘 迈从 K87 (2.4G)": 91, "耳机 QCY Era 7": 100}

        a = _fake_agent({})
        a.devices_snapshot = snap
        s = snap()                                   # 菜单构建的第一次读取
        self.assertEqual(a._icon_display_name(s), "耳机 QCY Era 7")
        self.assertEqual(a._icon_target(s), ("耳机 QCY Era 7", 100, False))
        self.assertEqual(len(calls), 1)              # 传 snap 后零次额外读取


class TrackedNameWired(unittest.TestCase):
    """24G 源的显示名跟着连接形态走（2026-10-12 修「有线条目点了无法勾选」）。

    devices_snapshot 会把插线键盘条目改写成 `(有线)`；若 `_tracked_name()`
    恒定拼 `(2.4G)`，手动选源在快照里查无此名 → 对勾永远不动。
    """

    def _a(self, tracked):
        a = object.__new__(A.Agent)
        a._tracked = tracked
        return a

    def test_wired_suffix_when_plugged(self):
        with mock.patch.object(R, "is_wired", lambda n, **k: True):
            self.assertEqual(self._a("24G:键盘 迈从 K87")._tracked_name(),
                             "键盘 迈从 K87 (有线)")

    def test_24g_suffix_when_not_plugged(self):
        with mock.patch.object(R, "is_wired", lambda n, **k: False):
            self.assertEqual(self._a("24G:键盘 迈从 K87")._tracked_name(),
                             "键盘 迈从 K87 (2.4G)")

    def test_is_wired_error_falls_back_to_24g(self):
        with mock.patch.object(R, "is_wired",
                               lambda n, **k: (_ for _ in ()).throw(OSError)):
            self.assertEqual(self._a("24G:键盘 迈从 K87")._tracked_name(),
                             "键盘 迈从 K87 (2.4G)")


class IconApplyNoDevices(unittest.TestCase):
    """tgt=None 时的图标与 tooltip（2026-10-12 用户报两处不合理）：

      * 全拔掉 → tooltip「鼠标 罗技 未知」误导（跟踪源那台根本不在）→
        应显示「未发现设备」；
      * 有条目但全读不出（关机充电伪 None / 毛刺 0）→ 不许回落跟踪源
        （跟踪源未必在快照里，正是「托盘显示耳机、列表却没有」的裂缝）。
    """

    def setUp(self):
        self._patch = mock.patch.object(R, "wired_bases", lambda *a, **k: set())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _agent(self, snap, ready=True):
        a = _fake_agent(snap)
        a._last_icon = None
        a._last_tooltip = ""
        a._last_raw = (None, False)
        a._ready = ready                     # 默认「启动后」态（已跑过一轮采集）
        a._icon = object()                   # 让 _set_icon 桩返回 True 即可
        cap = {}
        a._set_icon = lambda png, tip: (cap.update(png=png, tip=tip) or True)
        a._is_charging = lambda name: False
        return a, cap

    def test_empty_snapshot_shows_no_devices(self):
        """全拔掉：图标 "?" + tooltip「未发现设备」。"""
        a, cap = self._agent({})
        self.assertTrue(a._apply_icon(None, False))
        self.assertEqual(cap["tip"], "未发现设备")
        self.assertIsNone(a._last_icon.percent)
        self.assertFalse(a._last_icon.warning)

    def test_all_unknown_shows_first_entry_honestly(self):
        """有条目但全读不出 → 显示快照第一台的 "?"，不冒充跟踪源。"""
        a, cap = self._agent({"键盘 LEOBOG Hi98C Pro (有线)": None})
        self.assertTrue(a._apply_icon(None, False))
        self.assertIn("键盘 LEOBOG Hi98C Pro (有线)", cap["tip"])
        self.assertIn("未知", cap["tip"])
        self.assertIsNone(a._last_icon.percent)


class BootIcon(unittest.TestCase):
    """启动首帧（2026-10-13 用户：启动后 5 秒空白 → "?" → 才 "--"）。

    契约：① 建托盘**同步**画 "--"（_paint_boot，不读硬件、不等蓝牙查询）；
    ② 没拿到过任何真读数之前，一律 "--" + tooltip「启动中…」，不许出 "?"；
    ③ 一旦吃到真读数 → 立刻换数字，并转为「未发现设备」语义。
    """

    def setUp(self):
        self._patch = mock.patch.object(R, "wired_bases", lambda *a, **k: set())
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _agent(self, snap):
        a = _fake_agent(snap)
        a._last_icon = None
        a._last_tooltip = ""
        a._last_raw = (None, False)
        a._ready = False
        a._icon = object()
        cap = {}
        a._set_icon = lambda png, tip: (cap.update(png=png, tip=tip) or True)
        a._is_charging = lambda name: False
        return a, cap

    def test_paint_boot_renders_dash_sync(self):
        """_paint_boot 同步出图：像素与 no_devices 规格一致，tooltip「启动中…」。

        桩掉 devices_snapshot（真读蓝牙要 4~25s）——它一旦被调用就是 bug。
        """
        from batbar import icons as I
        a, cap = self._agent({})

        def boom(*_a, **_k):
            raise AssertionError("首帧不许读快照（会把托盘拖成几秒空白）")
        a.devices_snapshot = boom
        self.assertTrue(a._paint_boot())
        self.assertEqual(cap["tip"], "启动中…")
        self.assertTrue(a._last_icon.no_devices)
        self.assertEqual(
            cap["png"],
            I.render_icon(I.IconSpec(None, False, A.ICON_SIZE,
                                     light_taskbar=a._last_icon.light_taskbar,
                                     no_devices=True)))

    def test_before_any_reading_shows_dash_not_question(self):
        """启动阶段快照里是幽灵条目（读不出值）→ 画 "--" 而不是 "?"。"""
        a, cap = self._agent({"键盘 迈从 K87 (2.4G)": None})
        self.assertTrue(a._apply_icon(None, False))
        self.assertTrue(a._last_icon.no_devices)
        self.assertEqual(cap["tip"], "启动中…")
        self.assertFalse(a._ready)

    def test_first_real_reading_flips_to_number(self):
        """拿到真读数 → 立刻从 "--" 换成数字，并置 _ready。"""
        a, cap = self._agent({"鼠标 罗技 G304": 90})
        self.assertTrue(a._apply_icon(None, False))
        self.assertTrue(a._ready)
        self.assertEqual(a._last_icon.percent, 90)
        self.assertIn("90", cap["tip"])

    def test_first_reading_does_not_wait_for_full_snapshot(self):
        """跟踪源已有读数 → 首帧直接用它画，不等蓝牙全量查询（4~25s）。

        桩成「读快照就报错」：一旦被调用说明首帧又被拖住了。
        """
        a, cap = self._agent({})
        a._tracked_read = lambda: (90, False, "ok")
        a._manual_name = None

        def boom(*_a, **_k):
            raise AssertionError("首帧不该等蓝牙全量快照")
        a.devices_snapshot = boom
        self.assertTrue(a._refresh_from_tracked())
        self.assertEqual(a._last_icon.percent, 90)
        self.assertTrue(a._ready)
        self.assertIn("90", cap["tip"])

    def test_manual_choice_not_hijacked_by_fast_path(self):
        """用户已手动选源 → 首帧快路径不许越过他的选择（走常规快照）。"""
        a, cap = self._agent({"耳机 QCY Era 7": 40})
        a._tracked_read = lambda: (90, False, "ok")
        a._manual_name = "耳机 QCY Era 7"
        a._manual_until = time.monotonic() + 60
        self.assertTrue(a._refresh_from_tracked())
        self.assertEqual(a._last_icon.percent, 40)

    def test_ready_then_empty_is_no_devices_not_booting(self):
        """已就绪后再空 → tooltip「未发现设备」，不再一直挂「启动中…」。"""
        a, cap = self._agent({"鼠标 罗技 G304": 90})
        a._apply_icon(None, False)
        a.devices_snapshot = lambda: {}
        self.assertTrue(a._apply_icon(None, False))
        self.assertEqual(cap["tip"], "未发现设备")

    def test_target_none_reads_snapshot_once(self):
        """_apply_icon 自读快照时只读一次、并复用给 _icon_target（同源）。"""
        calls = []

        def snap():
            calls.append(1)
            return {}

        a, _cap = self._agent({})
        a.devices_snapshot = snap
        a._apply_icon(None, False)
        self.assertEqual(len(calls), 1)


class ManualRefreshForcesBT(unittest.TestCase):
    """「立即刷新」必须强刷蓝牙源（含连接态）——耳机刚连上时 15s 连接态
    缓存 + 20s TTL 会让它进不了列表，用户点了刷新就该看到。

    bug 13（2026-10-13）后结构升级：蓝牙连接态双向快判前置（sync_now，
    WinRT classic∪le ~0.6s，一次查询同时摘离线 + 加上线）→ CIM 单枪快查
    （quick=True）收尾；与 2.4G 扫描并行；每步完成即时重绘（_repaint），
    不等全部查询结束。"""

    def test_manual_refresh_pipeline(self):
        import types

        from batbar import poll24 as P
        a = object.__new__(A.Agent)
        calls = []
        a._push_fresh_read = lambda: calls.append("push")
        a._bt_battery = lambda: types.SimpleNamespace(
            sync_now=lambda: calls.append("sync") or ([], []),
            force_refresh=lambda quick=False: calls.append(
                ("bt", quick)))
        a._repaint = lambda: calls.append("redraw")
        with mock.patch.object(P, "scan_now", lambda: calls.append("scan")):
            a._manual_refresh()
        # 顺序断言只锁「先后依赖」：push 最先、收尾 repaint 最后；
        # 蓝牙步与 2.4G 步是并行线程，内部相对顺序不锁死。
        self.assertEqual(calls[0], "push")
        self.assertEqual(calls[-1], "redraw")
        self.assertIn("sync", calls)                # 双向快判（bug 15）
        self.assertIn(("bt", True), calls)          # CIM 单枪快查
        self.assertIn("scan", calls)
        self.assertGreaterEqual(calls.count("redraw"), 3)  # 每步都即时重绘

    def test_manual_refresh_tolerates_source_failures(self):
        """任一数据源炸掉不影响其余各路 + 收尾重绘（菜单线程是 daemon）。"""
        import types

        from batbar import poll24 as P
        a = object.__new__(A.Agent)
        calls = []
        a._push_fresh_read = lambda: None
        a._bt_battery = lambda: types.SimpleNamespace(
            sync_now=lambda: (_ for _ in ()).throw(
                RuntimeError("usb busy")),
            force_refresh=lambda quick=False: (_ for _ in ()).throw(
                RuntimeError("timeout")))
        a._repaint = lambda: calls.append("redraw")
        with mock.patch.object(P, "scan_now", lambda: calls.append("scan")):
            a._manual_refresh()
        self.assertIn("scan", calls)                # 2.4G 不受蓝牙故障牵连
        self.assertGreaterEqual(calls.count("redraw"), 3)


if __name__ == "__main__":
    unittest.main()
