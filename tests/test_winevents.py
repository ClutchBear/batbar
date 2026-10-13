"""设备/任务栏事件监听（winevents + agent 接线）测试。

覆盖 2026-10-13 新增的事件驱动：
  * `EventDebouncer` 去抖语义（一次插拔的 10+ 条连发只触发一次）
  * `classify` 子类型映射
  * Agent 的三个回调是否接到正确的刷新路径
  * `_start_theme_watch` 的窗口过程确实分派 WM_DEVICECHANGE / WM_TASKBARCREATED

全 stdlib、无需硬件（WNDPROC 用假 hwnd 直接调，不建真窗口）。
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from batbar import winevents as W  # noqa: E402


class DebouncerSemantics(unittest.TestCase):
    """EventDebouncer：一波连发只在静默边界触发一次。"""

    def test_single_event_fires_after_quiet(self):
        d = W.EventDebouncer(0.6)
        d.bump(100.0)
        self.assertFalse(d.due(100.1))     # 未到静默窗口
        self.assertFalse(d.due(100.59))
        self.assertTrue(d.due(100.6))      # 边界即触发
        self.assertFalse(d.due(101.0))     # 已消费，不重复

    def test_burst_collapses_to_one(self):
        """实测场景复现：一次插拔 12 条 NODES_CHANGED 挤在 1 秒内 → 只触发一次。"""
        d = W.EventDebouncer(0.6)
        for i in range(12):                # 模拟连发
            d.bump(100.0 + i * 0.02)
        # 连发期间任何时刻都不触发
        for i in range(12):
            self.assertFalse(d.due(100.0 + i * 0.02))
        # 最后一帧（100.22）之后静默 0.6s → 触发一次
        self.assertFalse(d.due(100.7))
        self.assertTrue(d.due(100.82))
        self.assertFalse(d.due(100.9))     # 且仅一次

    def test_no_event_no_fire(self):
        d = W.EventDebouncer(0.6)
        self.assertFalse(d.due(999.0))
        self.assertFalse(d.pending)

    def test_pending_and_deadline(self):
        d = W.EventDebouncer(0.6)
        self.assertIsNone(d.next_deadline())
        d.bump(50.0)
        self.assertTrue(d.pending)
        self.assertAlmostEqual(d.next_deadline(), 50.6)

    def test_second_wave_fires_again(self):
        """两波分离的事件各触发一次。"""
        d = W.EventDebouncer(0.6)
        d.bump(10.0)
        self.assertTrue(d.due(10.6))
        d.bump(20.0)                       # 第二波
        self.assertFalse(d.due(20.1))
        self.assertTrue(d.due(20.6))


class ClassifySubtypes(unittest.TestCase):
    """WM_DEVICECHANGE 的 wparam → 语义标签。"""

    def test_known_subtypes(self):
        self.assertEqual(W.classify(0x8000), "arrival")
        self.assertEqual(W.classify(0x8004), "remove")
        self.assertEqual(W.classify(0x0007), "nodes")

    def test_unrelated_ignored(self):
        # DBT_DEVICEQUERYREMOVE 等我们不管的子类型 → None（不触发刷新）
        self.assertIsNone(W.classify(0x8001))
        self.assertIsNone(W.classify(0x0000))


def _raw_mouse(us_flags=0, buttons=0, dx=0, dy=0, dw_type=W.RIM_TYPEMOUSE,
               size=48):
    """按真机实测的 x64 布局拼一条 RAWINPUT（用于离线单测）。

    布局（`_tmp/probe_rawinput_size.py` 实测）：header 24B + RAWMOUSE 24B。
      header: dwType(0) dwSize(4) hDevice(8) wParam(16)
      mouse : usFlags(24) pad(26) ulButtons(28) ulRawButtons(32)
              lLastX(36,signed) lLastY(40,signed) ulExtra(44)
    """
    import struct
    header = struct.pack("<IIQQ", dw_type, size, 0, 0)
    mouse = struct.pack("<HHIIiiI", us_flags, 0, buttons, 0, dx, dy, 0)
    assert len(header) == W.RAWINPUTHEADER_SIZE
    assert len(mouse) == W.RAWMOUSE_SIZE
    return header + mouse


class RawInputDecode(unittest.TestCase):
    """RAWINPUT 纯逻辑解码（2026-10-13 鼠标唤醒用）。

    这一层最容易踩的坑：**按键位在 `ulButtons` 而不是 `usFlags`**。
    若读错字段，「纯点击（光标不动）」会被判成无活动 ⇒ 鼠标点击无法唤醒条目。
    下面的用例把这几个字段的归属钉死。
    """

    def test_pure_move(self):
        info = W.decode_rawinput(_raw_mouse(dx=5, dy=-3))
        self.assertIsNotNone(info)
        self.assertEqual(info["kind"], "move")
        self.assertEqual((info["dx"], info["dy"]), (5, -3))

    def test_button_uses_ulbuttons_not_usflags(self):
        """★ 核心回归：按键置位在 ulButtons，usFlags 保持 0。"""
        for flag in (W.MOUSE_BUTTON_1_DOWN, W.MOUSE_BUTTON_1_UP,
                     W.MOUSE_BUTTON_2_DOWN, W.MOUSE_BUTTON_2_UP,
                     W.MOUSE_BUTTON_3_DOWN, W.MOUSE_BUTTON_5_UP):
            info = W.decode_rawinput(_raw_mouse(buttons=flag))
            self.assertEqual(info["kind"], "button", f"flag={flag:#06x}")
            self.assertEqual(info["flags"], 0)      # usFlags 未被误用

    def test_wheel_takes_priority(self):
        """滚轮位（0x0400/0x0800）判为 wheel —— 即使同时有按键位。"""
        self.assertEqual(
            W.decode_rawinput(_raw_mouse(buttons=W.MOUSE_WHEEL))["kind"], "wheel")
        self.assertEqual(
            W.decode_rawinput(_raw_mouse(buttons=W.MOUSE_HWHEEL))["kind"], "wheel")
        self.assertEqual(
            W.decode_rawinput(_raw_mouse(buttons=W.MOUSE_WHEEL | 0x0001))["kind"],
            "wheel")

    def test_absolute_flag_counts_as_move(self):
        info = W.decode_rawinput(_raw_mouse(us_flags=W.MOUSE_MOVE_ABSOLUTE))
        self.assertEqual(info["kind"], "move")

    def test_zero_delta_is_other(self):
        """零位移空帧 → other，且不算活动（不该催探测）。"""
        info = W.decode_rawinput(_raw_mouse())
        self.assertEqual(info["kind"], "other")
        self.assertFalse(W.rawinput_activity(_raw_mouse()))

    def test_rejects_keyboard_hid_and_short(self):
        self.assertIsNone(
            W.decode_rawinput(_raw_mouse(dw_type=W.RIM_TYPEKEYBOARD)))
        self.assertIsNone(W.decode_rawinput(_raw_mouse(dw_type=W.RIM_TYPEHID)))
        # 短于 header+mouse 一律 None（含边界 47）
        raw = _raw_mouse(buttons=0x0001)
        for n in (0, 8, W.RAWINPUTHEADER_SIZE, 47):
            self.assertIsNone(W.decode_rawinput(raw[:n]), f"len={n}")

    def test_is_mouse_input(self):
        self.assertTrue(W.is_mouse_input(_raw_mouse()))
        self.assertFalse(W.is_mouse_input(_raw_mouse(dw_type=W.RIM_TYPEKEYBOARD)))
        self.assertFalse(W.is_mouse_input(b""))
        self.assertFalse(W.is_mouse_input(b"\x00" * 7))     # < 8 字节

    def test_activity_semantics(self):
        """活动 = 有按键/滚轮，或有真实位移；空帧不算。"""
        self.assertTrue(W.rawinput_activity(_raw_mouse(dx=1)))
        self.assertTrue(W.rawinput_activity(_raw_mouse(buttons=W.MOUSE_BUTTON_1_DOWN)))
        self.assertTrue(W.rawinput_activity(_raw_mouse(buttons=W.MOUSE_WHEEL)))
        self.assertFalse(W.rawinput_activity(_raw_mouse()))
        self.assertFalse(W.rawinput_activity(b"short"))

    def test_debounce_window_shorter_than_device(self):
        """鼠标唤醒窗口要比设备事件短（跟手），但必须 >0。"""
        self.assertGreater(W.RAWINPUT_DEBOUNCE_SECONDS, 0)
        self.assertLess(W.RAWINPUT_DEBOUNCE_SECONDS, W.DEBOUNCE_SECONDS)


class AgentCallbacks(unittest.TestCase):
    """Agent 的三个回调接线（不碰真硬件）。"""

    def setUp(self):
        from batbar import agent as A
        self.A = A

    def test_device_change_spawns_refresh_thread(self):
        """设备变化 → 守护线程跑 _refresh_from_tracked（不阻塞消息循环）。"""
        a = object.__new__(self.A.Agent)
        calls = []
        a._refresh_from_tracked = lambda: calls.append("refresh")
        t = a._on_device_change()
        # 回调本身立即返回，实际刷新在守护线程
        import threading
        # 等到线程池跑完（join 最多 2s）
        deadline = 0
        while not calls and deadline < 200:
            import time
            time.sleep(0.01)
            deadline += 1
        self.assertEqual(calls, ["refresh"])

    def test_device_change_swallows_refresh_error(self):
        """刷新内部抛异常不能把消息循环带崩（线程里兜底）。"""
        a = object.__new__(self.A.Agent)

        def boom():
            raise RuntimeError("usb busy")

        a._refresh_from_tracked = boom
        a._on_device_change()      # 不应抛
        import time
        time.sleep(0.1)

    def test_taskbar_recreated_noop_without_icon(self):
        a = object.__new__(self.A.Agent)
        a._icon = None
        a._on_taskbar_recreated()  # 没图标时静默返回，不抛

    def test_taskbar_recreated_toggles_visible(self):
        """有图标 → 关再开 visible，触发 NIM_DELETE + NIM_ADD 重建。"""
        a = object.__new__(self.A.Agent)
        seq = []

        class FakeIcon:
            def __init__(self):
                self._v = True

            @property
            def visible(self):
                return self._v

            @visible.setter
            def visible(self, val):
                self._v = val
                seq.append(val)

        a._icon = FakeIcon()
        a._on_taskbar_recreated()
        import time
        time.sleep(0.5)            # 内部 sleep 0.2s + 线程调度
        self.assertEqual(seq, [False, True])


class WindowProcDispatch(unittest.TestCase):
    """_start_theme_watch 的窗口过程是否真的分派设备/任务栏消息。

    直接提取内部 `_wnd_proc` 不易（闭包在 `_loop` 里），改为验证**契约**：
    agent 源码中窗口过程必须处理这三个消息常量。这是防回归的静态检查
    （真窗口的消息投递已由 `_tmp/probe_device_events2.py` 实测证明）。
    """

    def test_wndproc_handles_device_and_taskbar(self):
        import inspect
        from batbar import agent as A
        src = inspect.getsource(A._start_theme_watch)
        self.assertIn("WM_DEVICECHANGE", src)
        self.assertIn("WM_TASKBARCREATED", src)
        self.assertIn("SetTimer", src)          # 去抖必须走定时器（不能 sleep）
        self.assertIn("KillTimer", src)
        self.assertIn("_device_listeners", src)
        self.assertIn("_taskbar_listeners", src)

    def test_wndproc_handles_rawinput_mouse(self):
        """窗口过程必须处理 WM_INPUT，且用独立定时器去抖（2026-10-13 鼠标唤醒）。"""
        import inspect
        from batbar import agent as A
        src = inspect.getsource(A._start_theme_watch)
        self.assertIn("WM_INPUT", src)
        self.assertIn("GetRawInputData", src)
        self.assertIn("MOUSE_TIMER_ID", src)
        self.assertIn("_mouse_listeners", src)
        self.assertIn("rawinput_activity", src)   # 空帧过滤

    def test_registers_raw_mouse_inputsink(self):
        """必须注册 RIDEV_INPUTSINK —— 否则无焦点窗口收不到全局鼠标输入。"""
        import inspect
        from batbar import agent as A
        src = inspect.getsource(A._register_raw_mouse)
        self.assertIn("RegisterRawInputDevices", src)
        self.assertIn("RIDEV_INPUTSINK", src)
        self.assertIn("_raw_input_keep", src)     # 结构体必须保活防 GC

    def test_registers_usb_notification(self):
        """必须调用 RegisterDeviceNotification 拿精确 ARRIVAL/REMOVECOMPLETE。"""
        from batbar import agent as A
        import inspect
        src = inspect.getsource(A._register_usb_notify)
        self.assertIn("RegisterDeviceNotificationW", src)
        self.assertIn("GUID", src.upper())


class ListenerRegistries(unittest.TestCase):
    """四个回调注册表互相独立（语义不同，不能混）。"""

    def test_separate_registries(self):
        from batbar import agent as A
        self.assertIsNot(A._theme_listeners, A._device_listeners)
        self.assertIsNot(A._device_listeners, A._taskbar_listeners)
        self.assertIsNot(A._taskbar_listeners, A._mouse_listeners)

    def test_agent_registers_all_four(self):
        """Agent.__init__ 必须把四个回调都挂上（源码级契约）。"""
        import inspect
        from batbar import agent as A
        src = inspect.getsource(A.Agent.__init__)
        self.assertIn("_theme_listeners.append", src)
        self.assertIn("_device_listeners.append", src)
        self.assertIn("_taskbar_listeners.append", src)
        self.assertIn("_mouse_listeners.append", src)


class MouseActivityGate(unittest.TestCase):
    """_on_mouse_activity 的门控：睡眠期不催、读数新鲜不催、否则催一次探测。

    RAWINPUT 频率极高（实测 ~33 条/s），门控是防止"一次拖动催几十次探测"的关键。
    """

    def _agent(self):
        from batbar import agent as A
        a = object.__new__(A.Agent)
        a._mouse_probe_calls = []
        a._sleep_until = 0.0
        a._last_info = None           # 无读数 ⇒ 视为陈旧
        a._last_read_ts = 0.0
        return a

    def test_skips_during_sleep_backoff(self):
        """主动睡眠期内（_sleep_until 未到）不催探测 —— 别打断退避。"""
        a = self._agent()
        import time
        a._sleep_until = time.monotonic() + 30.0
        a._probe_mouse = lambda: a._mouse_probe_calls.append("probe")
        a._on_mouse_activity()
        self.assertEqual(a._mouse_probe_calls, [])
        # 且不得清掉 _sleep_until（不碰退避状态）
        self.assertGreater(a._sleep_until, time.monotonic())

    def test_skips_when_reading_fresh(self):
        """读数仍新鲜（≤ MOUSE_FRESH）→ 不催（省一次硬件读）。"""
        a = self._agent()
        import time
        a._last_info = {"name": "G304", "percent": 90}   # 有条目 → 新鲜
        a._last_read_ts = time.monotonic()
        a._probe_mouse = lambda: a._mouse_probe_calls.append("probe")
        a._on_mouse_activity()
        time.sleep(0.1)
        self.assertEqual(a._mouse_probe_calls, [])

    def test_spawns_probe_when_stale(self):
        """不在睡眠、读数已陈旧 → 起线程跑一次 _probe_mouse。"""
        a = self._agent()
        import time
        a._probe_mouse = lambda: a._mouse_probe_calls.append("probe")
        a._on_mouse_activity()
        deadline = 0
        while not a._mouse_probe_calls and deadline < 200:
            time.sleep(0.01)
            deadline += 1
        self.assertEqual(a._mouse_probe_calls, ["probe"])


class BootOrderDarkMenus(unittest.TestCase):
    """_enable_dark_menus 必须在日志装好**之后**调用。

    2026-10-13 踩坑：它原先在 main() 顶部、`applog.setup()` 之前跑，导致
    「RAWINPUT 鼠标监听已注册」这类日志全部落在未配置的 logger 上丢失 ——
    出了事只能盲猜。这里静态钉死顺序。
    """

    def test_dark_menus_after_log_setup(self):
        import inspect
        from batbar import agent as A
        src = inspect.getsource(A.main)
        i_log = src.index("applog.setup")
        i_dark = src.index("_enable_dark_menus")
        self.assertLess(i_log, i_dark,
                        "applog.setup 必须早于 _enable_dark_menus")

    def test_dark_menus_before_agent_construction(self):
        """但仍要在建托盘菜单（Agent 构造）之前 —— 激活上下文得先生效。"""
        import inspect
        from batbar import agent as A
        src = inspect.getsource(A.main)
        i_dark = src.index("_enable_dark_menus")
        i_agent = src.index("Agent(")
        self.assertLess(i_dark, i_agent)


if __name__ == "__main__":
    unittest.main()
