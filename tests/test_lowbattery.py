"""低电量提醒状态机：去抖 / 迟滞 / 充电有线豁免 / 单发解除布防 / 弹前实时复核。"""

from __future__ import annotations

import os
import unittest

from batbar import lowbattery as LB


class Feed(unittest.TestCase):
    def setUp(self):
        LB.reset()

    def _w(self, **kw):
        return LB.LowBatteryWatcher(**kw)

    def test_single_low_read_does_not_notify(self):
        """单次低读数不弹（去抖：需连续 confirm 次）。"""
        w = self._w(confirm=3)
        self.assertFalse(w.feed("LEOBOG 键盘", 10))

    def test_three_consecutive_low_reads_notify(self):
        """连续 3 次 ≤阈值才触发一次。"""
        w = self._w(confirm=3)
        self.assertFalse(w.feed("K", 10))
        self.assertFalse(w.feed("K", 9))
        self.assertTrue(w.feed("K", 8))

    def test_glint_reset_breaks_streak(self):
        """中间夹一次正常读数 → 计数归零，毛刺 0 帧不累积。"""
        w = self._w(confirm=3)
        w.feed("K", 10)
        w.feed("K", 0)  # 孤立 0 帧（链路切换毛刺）
        w.feed("K", 80)  # 正常 → 归零
        w.feed("K", 10)  # 重新计数，前功尽弃
        self.assertFalse(w.feed("K", 9))

    def test_disarms_after_notify_until_rearm(self):
        """触发后解除布防：仍低也不再弹，直到回升到 threshold+gap 才重新布防。"""
        thr = LB.threshold()
        gap = LB.DEFAULT_RESET_GAP
        w = self._w(confirm=3)
        for _ in range(3):
            w.feed("K", 5)  # 第三次触发
        self.assertFalse(w.feed("K", 5))  # 已解除，不再弹
        # 迟滞带内（thr < pct < thr+gap）不重新布防
        w.feed("K", thr + 1)
        for _ in range(3):
            r = w.feed("K", 5)
        self.assertFalse(r)  # 仍未布防 → 不弹
        # 回升到 threshold+gap 才重新布防
        w.feed("K", thr + gap)
        for _ in range(2):
            w.feed("K", 5)
        self.assertTrue(w.feed("K", 4))  # 重新布防后再次达标 → 弹

    def test_charging_exempt(self):
        """充电中豁免：绝不弹，且撤销计数。"""
        w = self._w(confirm=3)
        w.feed("K", 5, charging=True)
        w.feed("K", 5, charging=True)
        self.assertFalse(w.feed("K", 5, charging=True))

    def test_wired_feed_exempt(self):
        """feed 传入 wired=True：豁免不弹。"""
        w = self._w(confirm=3)
        w.feed("K", 5, wired=True)
        w.feed("K", 5, wired=True)
        self.assertFalse(w.feed("K", 5, wired=True))

    def test_none_read_reset(self):
        """读不到（None）撤销计数，不弹。"""
        w = self._w(confirm=3)
        w.feed("K", 10)
        w.feed("K", None)
        self.assertFalse(w.feed("K", 10))  # None 归零后重新计数，未达 3

    def test_realtime_wired_check_blocks_false_alarm(self):
        """弹前 wired_check 实时复核发现插线 → 抑制假警报并重新布防。"""
        calls = {"n": 0}

        def wired(name):
            calls["n"] += 1
            return True  # 模拟：即将弹时实时查到「其实插着线」

        w = self._w(confirm=3, wired_check=wired)
        w.feed("K", 5)
        w.feed("K", 5)
        self.assertFalse(w.feed("K", 5))  # 达标但被 wired_check 拦下
        self.assertEqual(calls["n"], 1)  # 只在真要弹时调一次

    def test_wired_check_only_called_on_trigger(self):
        """wired_check 不在常规低读数里跑（只在达阈值那一瞬）。"""
        calls = {"n": 0}

        def wired(name):
            calls["n"] += 1
            return False

        w = self._w(confirm=3, wired_check=wired)
        w.feed("K", 5)
        w.feed("K", 5)  # 未达 confirm，不该查
        self.assertEqual(calls["n"], 0)
        self.assertTrue(w.feed("K", 5))  # 第三次：查一次 → 未有线 → 弹
        self.assertEqual(calls["n"], 1)

    def test_threshold_env_override(self):
        import os

        os.environ["BATBAR_LOWBAT_THRESHOLD"] = "30"
        try:
            LB.reset()
            w = self._w(confirm=1)
            # 阈值 30，读到 25 应算低
            self.assertTrue(w.feed("K", 25))
        finally:
            os.environ.pop("BATBAR_LOWBAT_THRESHOLD", None)


class GateSuppression(unittest.TestCase):
    """全屏/游戏门控（2026-10-13）：gate 返回 False 时不弹。

    语义要点：被抑制**仍然算已触发**（单发语义不变，不重复骚扰），但也不补弹。
    """

    def setUp(self):
        LB.reset()
        os.environ["BATBAR_LOWBAT_TOAST"] = "1"

    def tearDown(self):
        os.environ.pop("BATBAR_LOWBAT_TOAST", None)

    def _icon(self, sent):
        class FakeIcon:
            def notify(self, title, msg):
                sent.append((title, msg))

        return FakeIcon()

    def test_gate_false_suppresses_delivery(self):
        """gate 返回 False → 不弹，且 feed 返回 False。"""
        sent = []
        w = LB.LowBatteryWatcher(icon_getter=lambda: self._icon(sent), confirm=1, gate=lambda n, p: False)
        self.assertFalse(w.feed("键盘", 5))
        self.assertEqual(sent, [])

    def test_gate_true_allows_delivery(self):
        sent = []
        w = LB.LowBatteryWatcher(icon_getter=lambda: self._icon(sent), confirm=1, gate=lambda n, p: True)
        self.assertTrue(w.feed("键盘", 5))
        self.assertEqual(len(sent), 1)

    def test_gate_receives_name_and_pct(self):
        seen = []
        w = LB.LowBatteryWatcher(
            icon_getter=lambda: None, confirm=1, gate=lambda n, p: seen.append((n, p)) or True
        )
        w.feed("键盘", 7)
        self.assertEqual(seen, [("键盘", 7)])

    def test_suppressed_still_single_shot(self):
        """★ 被抑制后不得因"没弹成"而反复触发（单发语义）—— 否则全屏期间
        每轮 feed 都算一次触发，退出全屏就堆一屏通知。"""
        calls = []

        def gate(n, p):
            calls.append(p)
            return False

        w = LB.LowBatteryWatcher(icon_getter=lambda: None, confirm=1, gate=gate)
        w.feed("K", 5)  # 触发一次 → 被抑制 → 解除布防
        for _ in range(10):
            self.assertFalse(w.feed("K", 5))  # 已解除，不重复
        self.assertEqual(calls, [5])  # gate 只被问过一次

    def test_gate_exception_treated_as_allow(self):
        """门控自己抛异常 → 按"允许弹"处理（宁可打扰也别丢提醒）。"""
        sent = []
        os.environ["BATBAR_LOWBAT_TOAST"] = "1"

        def boom(n, p):
            raise RuntimeError("quns down")

        w = LB.LowBatteryWatcher(icon_getter=lambda: self._icon(sent), confirm=1, gate=boom)
        self.assertTrue(w.feed("键盘", 5))
        self.assertEqual(len(sent), 1)

    def test_no_gate_behaves_as_before(self):
        """不传 gate → 行为与改动前完全一致。"""
        sent = []
        w = LB.LowBatteryWatcher(icon_getter=lambda: self._icon(sent), confirm=1)
        self.assertTrue(w.feed("键盘", 5))
        self.assertEqual(len(sent), 1)

    def test_gate_only_consulted_at_delivery(self):
        """gate 不应在 feed 的**计数阶段**被调用（否则全屏切换会断掉计数）。"""
        calls = []
        w = LB.LowBatteryWatcher(
            icon_getter=lambda: None, confirm=3, gate=lambda n, p: calls.append(1) or True
        )
        w.feed("K", 5)  # 第 1 次：未达 confirm，不该问 gate
        w.feed("K", 5)  # 第 2 次：同上
        self.assertEqual(calls, [])
        w.feed("K", 5)  # 第 3 次：达标 → 投递前才问
        self.assertEqual(calls, [1])

    def test_singleton_get_passes_gate(self):
        """LB.get(gate=...) 要把 gate 接上单例。"""
        g = lambda n, p: False  # noqa: E731
        w = LB.get(icon_getter=lambda: None, gate=g)
        self.assertIs(w._gate, g)


class Deliver(unittest.TestCase):
    def setUp(self):
        LB.reset()
        # toast 默认已关（红字图标是主通道）；这里测投递机制，显式打开
        os.environ["BATBAR_LOWBAT_TOAST"] = "1"

    def tearDown(self):
        os.environ.pop("BATBAR_LOWBAT_TOAST", None)

    def test_toast_default_off(self):
        """2026-10-12 起默认不弹 toast（用户不看小铃铛；红字图标替代）。"""
        os.environ.pop("BATBAR_LOWBAT_TOAST", None)  # 清掉 setUp 打开的开关
        sent = []

        class FakeIcon:
            def notify(self, title, msg):
                sent.append((title, msg))

        w = LB.LowBatteryWatcher(icon_getter=lambda: FakeIcon(), confirm=1)
        w.feed("键盘", 5)
        self.assertEqual(sent, [])
        # 打开开关后同一路径应送达
        os.environ["BATBAR_LOWBAT_TOAST"] = "1"
        w.feed("K2", 6)  # 新设备、布防态默认 True
        self.assertEqual(len(sent), 1)

    def test_notify_called_via_icon_getter(self):
        """触发时经 icon_getter 拿到托盘并调 notify。"""
        sent = []

        class FakeIcon:
            def notify(self, title, msg):
                sent.append((title, msg))

        w = LB.LowBatteryWatcher(icon_getter=lambda: FakeIcon(), confirm=1)
        w.feed("键盘", 5)
        self.assertEqual(len(sent), 1)
        self.assertIn("键盘", sent[0][0])
        self.assertIn("5%", sent[0][1])

    def test_notify_with_callback_signature(self):
        """部分后端 notify 要求回调参数：TypeError 兜底重试一次。"""
        calls = {"n": 0}

        class StrictIcon:
            def notify(self, title, msg, cb):  # 强制三参
                calls["n"] += 1

        w = LB.LowBatteryWatcher(icon_getter=lambda: StrictIcon(), confirm=1)
        w.feed("K", 5)  # 第一次 notify(title,msg) 抛 TypeError → 补回调重试
        self.assertEqual(calls["n"], 1)

    def test_notify_exception_swallowed(self):
        """notify 抛异常不能冒泡（弹通知失败绝不影响监控）。"""

        class BoomIcon:
            def notify(self, *a, **k):
                raise RuntimeError("boom")

        w = LB.LowBatteryWatcher(icon_getter=lambda: BoomIcon(), confirm=1)
        # 不抛即通过
        self.assertTrue(w.feed("K", 5))

    def test_no_icon_getter_is_fine(self):
        """无 icon_getter（纯判定测试）：feed 返回 True 但不弹、不崩。"""
        w = LB.LowBatteryWatcher(confirm=1)
        self.assertTrue(w.feed("K", 5))


class PickDisplay(unittest.TestCase):
    """pick_display：图标常显「电量最低的那台」（2026-10-12 用户拍板）。"""

    def test_picks_lowest_without_threshold(self):
        """不看阈值：60% 是全场最低就显示它（阈值只管变不变红）。"""
        snap = {"耳机 西圣 XISEM-Air": 60, "耳机 QCY Era 7": 100, "键盘 迈从 K87": 90}
        self.assertEqual(LB.pick_display(snap), ("耳机 西圣 XISEM-Air", 60))

    def test_excludes_wired_and_charging(self):
        snap = {"键盘 LEOBOG Hi98C Pro": 8, "耳机 西圣 XISEM-Air": 60}
        self.assertEqual(
            LB.pick_display(snap, wired_bases={"键盘 LEOBOG Hi98C Pro"}), ("耳机 西圣 XISEM-Air", 60)
        )
        self.assertEqual(
            LB.pick_display(snap, charging_names={"键盘 LEOBOG Hi98C Pro"}), ("耳机 西圣 XISEM-Air", 60)
        )

    def test_excludes_zero_and_none(self):
        snap = {"毛刺": 0, "未知": None, "耳机": 45}
        self.assertEqual(LB.pick_display(snap), ("耳机", 45))
        self.assertIsNone(LB.pick_display({"毛刺": 0, "未知": None}))

    def test_all_excluded_returns_none(self):
        self.assertIsNone(LB.pick_display({"键盘": 8}, wired_bases={"键盘"}))


class PickLow(unittest.TestCase):
    """pick_low 纯函数：阈值内的低电判定（pick_display + 阈值过滤）。"""

    def test_picks_lowest(self):
        snap = {"耳机": 10, "键盘": 12, "鼠标": 90}
        self.assertEqual(LB.pick_low(snap, 15), ("耳机", 10))

    def test_above_threshold_is_not_low(self):
        """60% 虽是最低但不是「低电」→ 不变红（pick_display 仍会显示它）。"""
        self.assertIsNone(LB.pick_low({"耳机": 60, "鼠标": 90}, 15))

    def test_excludes_zero_and_none(self):
        """0% 是 2.4G 毛刺、None 无读数，都不算低电。"""
        snap = {"毛刺设备": 0, "未知设备": None, "耳机": 10}
        self.assertEqual(LB.pick_low(snap, 15), ("耳机", 10))
        self.assertIsNone(LB.pick_low({"毛刺设备": 0, "未知设备": None}, 15))

    def test_excludes_wired(self):
        snap = {"键盘": 10}
        self.assertIsNone(LB.pick_low(snap, 15, wired_bases={"键盘"}))

    def test_no_low_device(self):
        self.assertIsNone(LB.pick_low({"鼠标": 90, "耳机": 16}, 15))


class WarningIconColor(unittest.TestCase):
    """红字图标渲染。"""

    def test_warning_uses_red(self):
        from batbar import icons as I
        from PIL import Image
        import io as _io

        spec = I.IconSpec(10, False, 32, light_taskbar=False, warning=True)
        png = I.render_icon(spec)
        img = Image.open(_io.BytesIO(png)).convert("RGBA")
        colors = {px for _, px in enumerate(img.getdata()) if px[3] > 0}
        self.assertIn(I.RED_ON_DARKTASKBAR, colors)
        self.assertNotIn(I.COLOR_ON_DARKTASKBAR, colors)

    def test_normal_keeps_mono(self):
        from batbar import icons as I
        from PIL import Image
        import io as _io

        spec = I.IconSpec(90, False, 32, light_taskbar=False, warning=False)
        png = I.render_icon(spec)
        img = Image.open(_io.BytesIO(png)).convert("RGBA")
        colors = {px for _, px in enumerate(img.getdata()) if px[3] > 0}
        self.assertIn(I.COLOR_ON_DARKTASKBAR, colors)
        self.assertNotIn(I.RED_ON_DARKTASKBAR, colors)


class NoBoltOnIcon(unittest.TestCase):
    """托盘图标不画充电闪电（2026-10-12 22:23 用户拍板：LEOBOG/K87 都不要）。

    契约 = charging=True 与 False 的 PNG **逐字节相同**（纯数字居中，布局
    不随充电态变）。菜单文字 ⚡ 与 tooltip「(充电中)」不受本契约约束。
    """

    def test_charging_pixel_identical_to_normal(self):
        from batbar import icons as I

        a = I.render_icon(I.IconSpec(80, False, 32, light_taskbar=True))
        b = I.render_icon(I.IconSpec(80, True, 32, light_taskbar=True))
        self.assertEqual(a, b, "充电态图标必须与常规态一模一样（无闪电）")

    def test_100_charging_same(self):
        from batbar import icons as I

        a = I.render_icon(I.IconSpec(100, True, 32, light_taskbar=False))
        b = I.render_icon(I.IconSpec(100, False, 32, light_taskbar=False))
        self.assertEqual(a, b)


class NoDevicesDash(unittest.TestCase):
    """读不出电量一律画 "--"，**任何时候都不画 "?"**（2026-10-13 用户定稿）。

    用户原话：「需求是启动就显示两个减号，任何时候都不能显示问号」。
    所以 `percent is None` 只有一种画法 —— `no_devices` 退化为纯语义标记
    （只影响 tooltip「启动中…」/「未发现设备」）。
    """

    def test_none_always_draws_dash(self):
        """no_devices 开关不影响像素：两种 None 都画 "--"。"""
        from batbar import icons as I

        a = I.render_icon(I.IconSpec(None, False, 32, no_devices=True))
        b = I.render_icon(I.IconSpec(None, False, 32, no_devices=False))
        self.assertEqual(a, b)

    def test_dash_differs_from_digits(self):
        from batbar import icons as I

        dash = I.render_icon(I.IconSpec(None, False, 32))
        n = I.render_icon(I.IconSpec(90, False, 32))
        self.assertNotEqual(dash, n)

    def test_key_includes_no_devices(self):
        """`no_devices` 仍进缓存键（语义不同 → 允许重绘一次，但像素相同）。"""
        from batbar import icons as I

        self.assertNotEqual(
            I.IconSpec(None, False, 32, no_devices=True).key(), I.IconSpec(None, False, 32).key()
        )


class Singleton(unittest.TestCase):
    def setUp(self):
        LB.reset()

    def test_get_returns_same_instance(self):
        a = LB.get()
        b = LB.get()
        self.assertIs(a, b)

    def test_get_late_binds_icon_getter(self):
        LB.get()  # 先建，无 getter
        ig = lambda: None
        w = LB.get(icon_getter=ig)  # 后补 getter
        self.assertIs(w._icon_getter, ig)


if __name__ == "__main__":
    unittest.main()
