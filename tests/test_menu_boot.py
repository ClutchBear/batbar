# -*- coding: utf-8 -*-
"""启动阶段的菜单必须「不读硬件」（2026-10-13）。

真凶链：pystray `Icon.run()` 的顺序是
    `_create_window()` → `_mark_ready()` → **`update_menu()`** →
    （队列通知 setup 线程）→ `visible = True` → `_show()`（NIM_ADD）
即**菜单构建同步发生在图标注册之前**。菜单回调若走 `devices_snapshot()`
（蓝牙 CIM 查询 4~25s + USB 枚举），图标就被卡在菜单后面 —— 表现为
「双击后 5~10 秒才看到托盘图标，且一出现就是数字」，`--` 首帧根本来不及看。

所以 `_menu_items()` 在 `not self._ready` 时必须返回静态清单，一个硬件
读取都不许发生；`_ready` 置位后再由 `_menu_ready()` 异步换真实清单。
"""
from __future__ import annotations

import unittest
from unittest import mock

import pystray

from batbar import agent as A


class _FakeIcon:
    WM_NOTIFY = 0x0401

    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.visible = False
        self.updates = 0
        # 复刻 pystray：绑定方法快照进分发表（_install_menu_refresh 要换这条）
        self._message_handlers = {self.WM_NOTIFY: self._on_notify}

    def update_menu(self):
        self.updates += 1

    def _on_notify(self, wparam, lparam):
        """pystray 的消息处理（钩子会包在它外面，见 _install_menu_refresh）。"""
        return None


def _build(ready: bool, snap=None, snapshot_raises: bool = False,
           shown: bool = True):
    """造一个最小 Agent 并跑 `_build_tray()`，返回 (agent, 捕获物)。

    `shown` = 托盘是否已注册（`_tray_shown`）。首次菜单构建发生在 NIM_ADD
    **之前**，那时必须是静态清单 —— 见 `BootMenuShown` 用例。
    """
    a = object.__new__(A.Agent)
    import threading

    a._ready = ready
    a._menu_swapped = False
    a._tray_shown = shown
    a._icon_lock = threading.RLock()
    a._icon = None
    a._last_icon = None
    a._last_tooltip = ""
    a._last_raw = (None, False)
    a._running = True
    a._tracked = "hidpp"
    a._manual_name = None
    a._clients = []
    a._tracked_name = lambda: "鼠标 罗技 G304"
    a._key_for_name = lambda _n: "hidpp"
    a._is_charging = lambda _n: False
    a._refresh_from_tracked = lambda: None
    a._broadcast = lambda _d: None
    if snapshot_raises:
        def boom():
            raise AssertionError("启动阶段菜单读了硬件（会把图标一起拖住）")
        a.devices_snapshot = boom
    else:
        a.devices_snapshot = lambda: dict(snap or {})

    cap: dict = {}

    def make_icon(name, image, title, menu):
        cap["icon"] = _FakeIcon(name, image, title, menu)
        cap["menu"] = menu
        return cap["icon"]

    with mock.patch.object(pystray, "Icon", make_icon), \
            mock.patch.object(A.threading, "Thread",
                              lambda *args, **kwargs: mock.Mock()):
        A.Agent._build_tray(a)
    return a, cap


def _items(cap) -> list:
    """把动态菜单求值成条目清单。

    注意用 `list(menu)`（`Menu.__iter__` → `_visible_items()`）而不是
    `menu(icon)` —— 后者是 pystray 的「激活整个菜单」，会真的去执行每个
    条目的 action（"立即刷新"就这么跑起来了）。
    """
    return list(cap["menu"])


class BootMenu(unittest.TestCase):
    def test_boot_menu_never_reads_hardware(self):
        """启动阶段：菜单求值一次都不许碰 devices_snapshot。"""
        a, cap = _build(ready=False, snapshot_raises=True)
        texts = [str(getattr(i, "text", i)) for i in _items(cap)]
        self.assertTrue(any("启动中" in t for t in texts), texts)
        self.assertTrue(any("退出" in t for t in texts), texts)

    def test_boot_menu_keeps_refresh_and_quit(self):
        """静态菜单也得能用：立即刷新 + 退出都在。"""
        a, cap = _build(ready=False, snapshot_raises=True)
        texts = " | ".join(str(getattr(i, "text", i)) for i in _items(cap))
        self.assertIn("立即刷新", texts)
        self.assertIn("退出", texts)

    def test_ready_menu_reads_snapshot(self):
        """已就绪：菜单回到真实清单（含设备行）。"""
        a, cap = _build(ready=True, snap={"耳机 QCY Era 7": 90})
        texts = " | ".join(str(getattr(i, "text", i)) for i in _items(cap))
        self.assertIn("耳机 QCY Era 7", texts)
        self.assertIn("90%", texts)

    def test_boot_paint_uses_dash_icon(self):
        """建托盘那一刻就把 `--` 画上去（`no_devices=True`）。"""
        a, cap = _build(ready=False, snapshot_raises=True)
        self.assertIsNotNone(a._last_icon)
        self.assertTrue(a._last_icon.no_devices)
        self.assertIsNone(a._last_icon.percent)
        self.assertEqual(a._last_tooltip, "启动中…")


class BootMenuShown(unittest.TestCase):
    """托盘注册之前，菜单构建**绝不许读硬件**（2026-10-13 10:25 用户报
    「双击后四五秒才出现电量数字」）。

    pystray 的 `run()` 顺序是「建窗口 → **建菜单** → setup(visible=True) →
    NIM_ADD」，所以首次建菜单发生在图标注册**之前**。而「等待首帧」会让
    `_ready` 提前置位 —— 若菜单门控只看 `_ready`，这次构建就会去读硬件
    （实测 ~3s），把 NIM_ADD 拖到 +3.63s。实测日志：
    `boot: 图标已注册（NIM_ADD）内容='耳机 QCY Era 7: 100%' 等待=0.61s +3.63s`。
    """

    def test_menu_is_static_before_tray_shown(self):
        a, cap = _build(ready=True, shown=False, snapshot_raises=True)
        texts = " | ".join(str(getattr(i, "text", i)) for i in _items(cap))
        self.assertIn("启动中", texts)          # 仍是静态清单
        self.assertNotIn("立即刷新  ", texts)

    def test_menu_reads_snapshot_after_shown(self):
        a, cap = _build(ready=True, shown=True, snap={"耳机 QCY Era 7": 90})
        texts = " | ".join(str(getattr(i, "text", i)) for i in _items(cap))
        self.assertIn("耳机 QCY Era 7", texts)

    def test_menu_stays_static_when_not_ready(self):
        """★ 2026-10-13 10:40 用户报的场景：无设备启动 → 中途打开鼠标。

        托盘已注册但还没拿到任何真读数时，菜单必须是静态的（那时读硬件也读不
        到东西）；等 `_ready` 置位后，弹出前的重建才会给出真实清单。
        """
        a, cap = _build(ready=False, shown=True, snapshot_raises=True)
        texts = " | ".join(str(getattr(i, "text", i)) for i in _items(cap))
        self.assertIn("启动中", texts)
        # `_ready` 置位（鼠标开机后拿到读数）→ 同一次构建就该给真实清单
        a._ready = True
        a.devices_snapshot = lambda: {"鼠标 罗技 G304": 90}
        texts = " | ".join(str(getattr(i, "text", i)) for i in _items(cap))
        self.assertIn("鼠标 罗技 G304", texts)


class MenuRefreshOnPopup(unittest.TestCase):
    """菜单必须**在每次弹出之前**重建（2026-10-13 10:40 用户报「托盘已显示
    电量，右键菜单却还是『启动中…』」）。

    pystray 的右键是 `_win32.Icon._on_notify` → `TrackPopupMenuEx(self._menu_handle)`
    —— **复用上次建好的 HMENU，不重建**。所以启动那次静态构建会一直生效，
    除非有人在弹出前把它换掉。而 pystray 在 `__init__` 里就把**绑定方法**
    存进了 `_message_handlers`（窗口过程按表分发），所以钩子必须换表里那一条，
    只改类属性没用 —— 这两个用例就是钉住这一点。
    """

    WM_NOTIFY = 0x0401

    def _icon(self, update_raises: bool = False):
        class FakeIcon:
            def __init__(self):
                self.updates = 0
                self.seen: list = []
                # 复刻 pystray：绑定方法快照进分发表
                self._message_handlers = {self.WM_NOTIFY: self._on_notify}

            WM_NOTIFY = 0x0401

            def update_menu(self):
                self.updates += 1
                if update_raises:
                    raise RuntimeError("menu gone")

            def _on_notify(self, wparam, lparam):
                self.seen.append(lparam)
                return "orig"

            def dispatch(self, lparam):
                """模拟窗口过程：按分发表调用。"""
                return self._message_handlers[self.WM_NOTIFY](0, lparam)

        return FakeIcon()

    def test_hook_rebuilds_before_popup(self):
        icon = self._icon()
        A._install_menu_refresh(icon)
        self.assertEqual(icon.dispatch(0x0205), "orig")     # WM_RBUTTONUP
        self.assertEqual(icon.updates, 1, "弹出前必须重建菜单")
        self.assertEqual(icon.seen, [0x0205], "原处理逻辑仍要执行")

    def test_hook_ignores_other_messages(self):
        icon = self._icon()
        A._install_menu_refresh(icon)
        icon.dispatch(0x0201)                               # WM_LBUTTONDOWN
        self.assertEqual(icon.updates, 0)

    def test_hook_is_idempotent(self):
        icon = self._icon()
        A._install_menu_refresh(icon)
        once = icon._message_handlers[self.WM_NOTIFY]
        A._install_menu_refresh(icon)
        self.assertIs(icon._message_handlers[self.WM_NOTIFY], once,
                      "重复安装不许叠加包装")

    def test_hook_survives_update_failure(self):
        icon = self._icon(update_raises=True)
        A._install_menu_refresh(icon)
        self.assertEqual(icon.dispatch(0x0205), "orig")     # 重建失败也要能弹出

    def test_hook_without_handlers_table_is_noop(self):
        """pystray 换了内部结构（没有分发表）→ 只告警，不许让托盘起不来。"""
        class Bare:
            def update_menu(self):
                pass
        A._install_menu_refresh(Bare())                     # 不抛


class StandaloneLoopDelegates(unittest.TestCase):
    """`--standalone` 的绘制必须走 `_apply_icon`（单一来源）。

    2026-10-13 用户报「触摸板移到减号上显示 **鼠标 罗技 G304: 未知**」：
    `_standalone_loop` 原来自己拼 `_tip_for(st.percent, charging)` —— 不传
    name → `_tip_for` 回落到**跟踪源名**，于是一台设备都没连时托盘提示却写着
    这个不存在的鼠标名。而且这条循环 0.5s 一次，比 `_bt_warm_loop`（~9s）密，
    会一直盖住 `_apply_icon` 写的「启动中…」。
    """

    def test_loop_delegates_to_apply_icon(self):
        import types

        a = object.__new__(A.Agent)
        a._running = True
        calls: list = []
        a._apply_icon = lambda pct, chg, snap=None: calls.append((pct, chg)) or True

        class FakePoller:
            def __init__(self, session):
                self.n = 0

            def due(self):
                return False

            def poll_once(self):
                pass

            def take_icon_update(self):
                self.n += 1
                if self.n == 1:
                    return types.SimpleNamespace(percent=None, info=None)
                a._running = False
                return None

        from batbar import state as S
        with mock.patch.object(S, "BatteryPoller", FakePoller), \
                mock.patch.object(A.time, "sleep", lambda _s: None):
            A.Agent._standalone_loop(a)
        self.assertEqual(calls, [(None, False)])

    def test_tip_for_has_single_call_site(self):
        """`_tip_for` 只许被 `_apply_icon` 调用（防止别处再拼出设备名）。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "src" / "batbar" / "agent.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("self._tip_for("), 2,
                         "新增了 _tip_for 调用点？托盘 tooltip 必须只有一个来源")


class WarmLoopResetsIcon(unittest.TestCase):
    """选不出目标时图标必须回到 `--`（不许留着上一台的数字）。

    否则「图标有、列表没有」会以另一种形式出现：设备全没了，菜单说
    「未发现设备」，图标却还挂着 90。
    """

    def test_icon_redrawn_when_no_target(self):
        from batbar import icons as I

        a = object.__new__(A.Agent)
        a._running = True
        a._icon = object()
        a._last_icon = I.IconSpec(90, False, 32)
        a._last_warn = ("鼠标 罗技 G304", 90, False)
        a._ticks = 0
        a._ready = True
        a._icon_target = lambda: None
        calls: list = []
        a._refresh_from_tracked = lambda: calls.append(1) or True
        a._probe_mouse = lambda: None
        a._feed_snapshot = lambda: None
        a._menu_ready = lambda: None
        a._bt_battery = lambda: mock.Mock(prewarm=lambda: None)

        def stop(_s):
            a._running = False

        with mock.patch.object(A.time, "sleep", stop):
            A.Agent._bt_warm_loop(a)
        self.assertTrue(calls, "选不出目标时必须重绘（让图标回到 --）")

    def test_icon_untouched_when_already_dash(self):
        """图标本来就是 `--`、目标也没了 → 不必重绘（省句柄重建）。"""
        from batbar import icons as I

        a = object.__new__(A.Agent)
        a._running = True
        a._icon = object()
        a._last_icon = I.IconSpec(None, False, 32, no_devices=True)
        a._last_warn = None
        a._ticks = 1                      # 避开 % 4 == 0 的定期重绘
        a._ready = True
        a._icon_target = lambda: None
        calls: list = []
        a._refresh_from_tracked = lambda: calls.append(1) or True
        a._probe_mouse = lambda: None
        a._feed_snapshot = lambda: None
        a._menu_ready = lambda: None
        a._bt_battery = lambda: mock.Mock(prewarm=lambda: None)

        def stop(_s):
            a._running = False

        with mock.patch.object(A.time, "sleep", stop):
            A.Agent._bt_warm_loop(a)
        self.assertEqual(calls, [])


class BootTooltipEndsOnFirstRound(unittest.TestCase):
    """首轮采集完成必须**当轮**把「启动中…」换成「未发现设备」。

    2026-10-13 09:38 用户报「只有一个鼠标时『启动中…』挂很久，多设备时几乎
    看不到」。根因：空快照下 `stale` 恒 False——启动占位图本来就是 `--`
    （percent=None），而 `stale` 比的是**图标数字**；`tgt` 与 `_last_warn`
    又都是 None ⇒ 重绘只能等 `_ticks % 4 == 0`（每轮 ~5s，约 20s 后）。
    多设备时 poller 那条 0.5s 循环顺手重绘，把这个缺口盖住了。
    """

    def _agent(self, ready: bool):
        from batbar import icons as I

        a = object.__new__(A.Agent)
        a._running = True
        a._icon = object()
        a._last_icon = I.IconSpec(None, False, 32, no_devices=True)   # 启动占位图
        a._last_tooltip = "启动中…"
        a._last_warn = None
        a._ticks = 0
        a._ready = ready
        a._icon_target = lambda: None          # 空快照 → 选不出目标
        a._menu_ready = lambda: None
        a._probe_mouse = lambda: None
        a._feed_snapshot = lambda: None
        a._bt_battery = lambda: mock.Mock(prewarm=lambda: None)
        return a

    def _run_rounds(self, a) -> list:
        calls: list = []
        a._refresh_from_tracked = lambda: calls.append(1) or True

        def stop(_s):
            a._running = False

        with mock.patch.object(A.time, "sleep", stop):
            A.Agent._bt_warm_loop(a)
        return calls

    def test_first_round_repaints_boot_tooltip(self):
        a = self._agent(ready=False)
        calls = self._run_rounds(a)
        self.assertTrue(calls, "首轮跑完必须重绘，否则「启动中…」要等 ~20s 才换")
        self.assertTrue(a._ready)

    def test_no_forced_repaint_when_already_ready(self):
        """已就绪 + 空快照 + 图标本来就是 `--` → 不必重绘（省句柄重建）。"""
        a = self._agent(ready=True)
        self.assertEqual(self._run_rounds(a), [])


class WaitFirstPaint(unittest.TestCase):
    """注册托盘图标前先等首帧内容（2026-10-13 09:49 / 10:05 用户两次报
    「6~7 秒才显示电量」）。

    实测 shell 在图标 add 之后的一段时间里**收下 NIM_MODIFY 却不重绘**
    （`probes/probe_tray_modify.py`：返回 1、GetLastError=0，托盘画面不动），
    而 **add 那一帧必然被渲染**（首帧 `--` 秒出即证）⇒ 把真读数赶在 add
    之前准备好，托盘一出现就是电量。等待必须有界：鼠标在睡/没设备时不能
    把启动拖住。
    """

    def test_ready_returns_immediately(self):
        import time

        a = object.__new__(A.Agent)
        a._ready = True
        t0 = time.monotonic()
        waited = A.Agent._wait_first_paint(a, 1.2)
        self.assertLess(time.monotonic() - t0, 0.1)
        self.assertLess(waited, 0.1)

    def test_waits_until_ready(self):
        import threading
        import time

        a = object.__new__(A.Agent)
        a._ready = False

        def flip():
            time.sleep(0.15)
            a._ready = True

        threading.Thread(target=flip, daemon=True).start()
        waited = A.Agent._wait_first_paint(a, 2.0)
        self.assertTrue(a._ready)
        self.assertGreater(waited, 0.1)

    def test_timeout_bounds_wait(self):
        """永远拿不到读数（鼠标在睡/没设备）→ 到点就走，不能拖住启动。"""
        import time

        a = object.__new__(A.Agent)
        a._ready = False
        t0 = time.monotonic()
        A.Agent._wait_first_paint(a, 0.3)
        dt = time.monotonic() - t0
        self.assertGreaterEqual(dt, 0.25)
        self.assertLess(dt, 1.0)


class _FakeTrayIcon:
    """记录 `visible` 切换与图标赋值的假托盘图标。"""

    def __init__(self, visible: bool = True, hide_raises: bool = False):
        object.__setattr__(self, "_visible", visible)
        object.__setattr__(self, "toggles", [])
        object.__setattr__(self, "icon_sets", 0)
        object.__setattr__(self, "_hide_raises", hide_raises)
        object.__setattr__(self, "title", None)

    @property
    def visible(self):
        return self._visible

    @visible.setter
    def visible(self, value):
        if value is False and self._hide_raises:
            raise RuntimeError("hide failed")
        self.toggles.append(value)
        object.__setattr__(self, "_visible", value)

    @property
    def icon(self):
        return None

    @icon.setter
    def icon(self, _value):
        object.__setattr__(self, "icon_sets", self.icon_sets + 1)


class IconReaddOnChange(unittest.TestCase):
    """内容变化必须**重新注册**（NIM_DELETE + NIM_ADD），不能只发 NIM_MODIFY。

    2026-10-13 实测（`probes/probe_tray_modify.py` + agent 日志时间线）：图标
    add 之后的**第一次** NIM_MODIFY 会被 shell 收下（返回 1、GetLastError=0）
    却**不重绘** —— 托盘画面停在 add 那一帧，要等下一次刷新（实测 ~6s，正好是
    `_bt_warm_loop` 那轮）才换。而 **add 本身必然被渲染**。这正是「日志显示
    +0.6s 就画好了、用户 6~7s 才看到电量」的全部原因。
    """

    def _agent(self, icon):
        import threading

        a = object.__new__(A.Agent)
        a._icon = icon
        a._icon_lock = threading.RLock()
        a._last_painted = None
        return a

    @staticmethod
    def _png(n: int) -> bytes:
        from batbar import icons as I
        return I.render_icon(I.IconSpec(n, False, 32, light_taskbar=False))

    def test_change_triggers_readd(self):
        icon = _FakeTrayIcon(visible=True)
        a = self._agent(icon)
        self.assertTrue(A.Agent._set_icon(a, self._png(90), "鼠标: 90%"))
        self.assertEqual(icon.toggles, [False, True], "必须 hide+show 一次")
        self.assertEqual(icon.icon_sets, 1)

    def test_unchanged_content_does_not_readd(self):
        """内容一模一样（如每 ~36s 的定期自愈重绘）→ 走普通 modify，不 toggle。

        自愈那枪的行为保持不变：它照旧重发一次图标（换新句柄），只是不必
        再 delete+add 一遍。
        """
        icon = _FakeTrayIcon(visible=True)
        a = self._agent(icon)
        png = self._png(90)
        A.Agent._set_icon(a, png, "鼠标: 90%")
        icon.toggles.clear()
        self.assertTrue(A.Agent._set_icon(a, png, "鼠标: 90%"))
        self.assertEqual(icon.toggles, [])
        self.assertEqual(icon.icon_sets, 2)      # 仍重发了一次图标

    def test_tooltip_only_change_still_readds(self):
        """只有文案变（`--` → 「未发现设备」）也要重新注册，否则文案不刷新。"""
        icon = _FakeTrayIcon(visible=True)
        a = self._agent(icon)
        png = self._png(90)
        A.Agent._set_icon(a, png, "启动中…")
        icon.toggles.clear()
        A.Agent._set_icon(a, png, "未发现设备")
        self.assertEqual(icon.toggles, [False, True])

    def test_invisible_icon_is_only_stored(self):
        """托盘还没注册（启动占位那一枪）→ 只存不画，不许 toggle。"""
        icon = _FakeTrayIcon(visible=False)
        a = self._agent(icon)
        self.assertTrue(A.Agent._set_icon(a, self._png(90), "启动中…"))
        self.assertEqual(icon.toggles, [])

    def test_icon_never_left_hidden_on_error(self):
        """hide 失败也要把图标恢复可见——绝不能把用户托盘图标弄没。"""
        icon = _FakeTrayIcon(visible=True, hide_raises=True)
        a = self._agent(icon)
        self.assertFalse(A.Agent._set_icon(a, self._png(90), "鼠标: 90%"))
        self.assertTrue(icon.visible, "异常后图标必须仍是可见的")


class _Recorder:
    """记录「锁是否被持有」的上下文管理器（替代真锁，便于断言）。"""

    def __init__(self):
        self.held = False
        self.entries = 0

    def __enter__(self):
        self.held = True
        self.entries += 1
        return self

    def __exit__(self, *exc):
        self.held = False
        return False


class IconUpdateSerialized(unittest.TestCase):
    """图标更新必须串行化（2026-10-13 实测 `WinError 1402 无效的光标句柄`）。

    pystray 的 `icon` setter 内部是「先 `_release_icon()` 销毁旧句柄、再
    `_assert_icon_handle()` 建新句柄」，而图标有 4 个更新来源线程 —— 交叉
    就会 1402。更新失败时图标**保留上一次的数字**，于是出现「托盘有电量、
    菜单却未发现设备」的劈叉。
    """

    @staticmethod
    def _png() -> bytes:
        from batbar import icons as I
        return I.render_icon(I.IconSpec(90, False, 32, light_taskbar=False))

    def test_set_icon_runs_inside_lock(self):
        a = object.__new__(A.Agent)
        rec = _Recorder()
        a._icon_lock = rec
        seen: dict = {}

        class Probe:
            title = None

            @property
            def icon(self):
                return None

            @icon.setter
            def icon(self, value):
                seen["locked"] = rec.held

        a._icon = Probe()
        self.assertTrue(A.Agent._set_icon(a, self._png(), "t"))
        self.assertTrue(seen.get("locked"),
                        "icon 赋值必须发生在 _icon_lock 保护区内")

    def test_set_icon_without_tray_returns_false(self):
        a = object.__new__(A.Agent)
        a._icon = None
        self.assertFalse(A.Agent._set_icon(a, b"", "t"))

    def test_concurrent_updates_do_not_raise(self):
        import threading as th

        a = object.__new__(A.Agent)
        a._icon_lock = th.RLock()

        class Probe:
            title = None

            @property
            def icon(self):
                return None

            @icon.setter
            def icon(self, value):
                pass

        a._icon = Probe()
        png = self._png()
        errs: list = []

        def worker():
            try:
                for _ in range(40):
                    A.Agent._set_icon(a, png, "t")
            except Exception as exc:       # noqa: BLE001
                errs.append(exc)

        threads = [th.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])


class CwdAnchor(unittest.TestCase):
    """启动锚定工作目录（2026-10-13）。

    注册表 `Run` 键 / 计划任务 / `restart-diag.bat` 拉起进程时 cwd 可能是
    `C:\\Windows\\System32`。当前所有落盘路径都从 `%LOCALAPPDATA%` 推导，
    今天不受影响；这一层是给「将来有人写相对路径」买的保险 —— 那种 bug
    在本机开发时永远复现不出来。

    锚点选 `sys.base_prefix` 而非 `sys.prefix`：venv 里后者指向 `.venv`，
    虽然也存在，但「Python 安装根」语义更稳。
    """

    def test_anchors_to_base_prefix_by_default(self):
        """默认锚到 `sys.base_prefix`，且必须**真的**调 os.chdir。"""
        import sys
        with mock.patch.object(A.os, "chdir") as chdir:
            got = A.anchor_cwd()
        chdir.assert_called_once()
        self.assertEqual(chdir.call_args[0][0], sys.base_prefix)
        # 返回值是「最终 cwd」，不是目标本身
        self.assertIsInstance(got, str)

    def test_explicit_target_wins(self):
        """测试可注入目标（本轮靠它在不真改 cwd 的前提下验证）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(A.os, "chdir") as chdir:
                A.anchor_cwd(d)
            self.assertEqual(chdir.call_args[0][0], d)

    def test_missing_target_is_ignored_not_fatal(self):
        """目标不存在 → 不 chdir、不抛异常（绝不让进程起不来）。"""
        missing = "Z:/definitely/not/here/qqq"
        with mock.patch.object(A.os, "chdir") as chdir:
            A.anchor_cwd(missing)
        chdir.assert_not_called()

    def test_chdir_failure_never_raises(self):
        """os.chdir 自己炸了也只能咽下去 —— 锚定失败不是致命错误。"""
        with mock.patch.object(A.os, "chdir", side_effect=OSError("denied")):
            A.anchor_cwd()          # 不抛即通过

    def test_returns_something_even_if_getcwd_breaks(self):
        """getcwd 也炸时仍要返回 str，调用方无需防空。"""
        with mock.patch.object(A.os, "chdir"), \
                mock.patch.object(A.os, "getcwd", side_effect=OSError("gone")):
            got = A.anchor_cwd("C:/Windows")
        self.assertIsInstance(got, str)

    def test_called_before_argparse_in_main(self):
        """源码级断言：锚定必须早于 argparse —— 任何文件访问都可能受 cwd 影响。

        用源码文本比对而不是跑 main()：main 会真的起托盘、真的占单实例锁。
        """
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "src" / "batbar" / "agent.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("anchor_cwd()"), 1,
                         "main() 里应恰有一处 anchor_cwd() 调用")
        body = src.split("def main():", 1)[1]
        i_anchor = body.index("anchor_cwd()")
        i_parse = body.index("ap.parse_args()")
        self.assertLess(i_anchor, i_parse,
                        "anchor_cwd() 必须在 ap.parse_args() 之前")

    # ---------- frozen（PyInstaller）分支 ----------

    def test_frozen_anchors_to_exe_dir_not_meipass(self):
        """打包后 `sys.base_prefix` 被指到 `_MEIPASS`，绝不能锚在那里。

        onefile 的 `_MEIPASS` 是 `%TEMP%\\_MEIxxxxxx`，Windows 不允许删除
        「正被某进程当作工作目录」的目录 ⇒ 锚在那里会让 bootloader 退出时的
        临时目录清理失败，TEMP 里 `_MEI*` 越堆越多。必须改锚 exe 所在目录。
        """
        import os
        import sys
        fake_exe = os.path.join("X:" + os.sep, "Apps", "batbar", "batbar.exe")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", fake_exe), \
                mock.patch.object(A.os.path, "isdir", return_value=True), \
                mock.patch.object(A.os, "chdir") as chdir:
            A.anchor_cwd()
        chdir.assert_called_once()
        self.assertEqual(chdir.call_args[0][0], os.path.dirname(fake_exe))
        self.assertNotEqual(chdir.call_args[0][0], getattr(sys, "base_prefix", ""))

    def test_frozen_exe_dir_unusable_is_not_fatal(self):
        """frozen 下锚定失败同样只能咽下去 —— 打包后更不能起不来。"""
        import sys
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(A.os, "chdir", side_effect=OSError("nope")):
            A.anchor_cwd()          # 不抛即通过

    def test_explicit_target_beats_frozen_default(self):
        """显式注入永远优先，否则 frozen 环境里没法做测试注入。"""
        import sys
        import tempfile
        with mock.patch.object(sys, "frozen", True, create=True):
            with tempfile.TemporaryDirectory() as d:
                with mock.patch.object(A.os, "chdir") as chdir:
                    A.anchor_cwd(d)
                self.assertEqual(chdir.call_args[0][0], d)


class ConsoleAttach(unittest.TestCase):
    """打包成 windowed exe 后没有控制台（2026-10-13 打包 exe 时补）。

    `sys.stdout is None` 时 CPython 的 `print()` 是**静默 no-op**（不报错）——
    于是 `batbar.exe --scan` 一声不吭，比抛异常更难查。这一层负责接回控制台。
    """

    def test_passthrough_when_stdout_present(self):
        """源码运行 / console 版 exe 本来就有 stdout → 直接 True，不做多余动作。"""
        self.assertTrue(A.attach_parent_console(attach=False))

    def test_false_when_attach_fails(self):
        """没有父控制台（双击运行）→ False，且绝不抛。"""
        import sys
        with mock.patch.object(sys, "stdout", None):
            self.assertFalse(A.attach_parent_console(attach=False))

    def test_success_path_never_raises(self):
        """附加成功要去开 `CONOUT$`：环境不给也必须咽下去。"""
        import sys
        with mock.patch.object(sys, "stdout", None):
            A.attach_parent_console(attach=True)      # 不抛即通过

    def test_called_before_log_setup_in_main(self):
        """必须先于 `applog.setup()`：安装器靠 stdout 是否为 None 决定装不装
        控制台 handler，晚了就永远没有控制台输出。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "src" / "batbar" / "agent.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("attach_parent_console()"), 1)
        body = src.split("def main():", 1)[1]
        # 用完整的调用形式匹配 —— 注释里也写着 applog.setup()，只匹配函数名
        # 会命中注释（这个坑在 test_migrate 里已经踩过一次）
        self.assertLess(body.index("attach_parent_console()"),
                        body.index("path = applog.setup("))


class ConsoleEncoding(unittest.TestCase):
    """中文 cmd（代码页 936 / cp936）下的编码地雷（2026-10-13 打包 exe 时发现）。

    往 cp936 的 stdout 写 emoji 会抛 `UnicodeEncodeError` 并**终止进程**。
    打包后的 exe 吃了个正着：`batbar.exe --scan` 跑到「✅ 可用写长度」那行就
    退出，rc=1，日志一片空白（print 不走 logging）。而 Git Bash / VS Code 的
    UTF-8 终端**永远复现不出来** —— 所以必须由测试来守住，不能靠手测。
    """

    def test_no_print_uses_non_gbk_chars(self):
        """src/ 里所有 print() 行只能出现 GBK 能编码的字符。"""
        from pathlib import Path
        src_dir = Path(__file__).resolve().parents[1] / "src"
        bad = []
        for p in sorted(src_dir.rglob("*.py")):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if "print(" not in line:
                    continue
                for ch in line:
                    try:
                        ch.encode("gbk")
                    except UnicodeEncodeError:
                        bad.append(f"{p.name}:{i} U+{ord(ch):04X}")
                        break
        self.assertEqual(bad, [], f"这些 print 在中文 cmd 上会崩：{bad}")

    def test_make_console_safe_never_raises(self):
        """兜底层跑在 main() 最前面，自己绝不能抛；重复调也要安全。"""
        A.make_console_safe()
        A.make_console_safe()

    def test_crash_hook_logs_traceback(self):
        """未捕获异常必须留下现场（windowed exe 里 traceback 本来无处可去）。"""
        import sys
        old = sys.excepthook
        try:
            A.install_crash_log()
            with mock.patch.object(A.log, "error") as err:
                try:
                    raise ValueError("boom-测试用")
                except ValueError:
                    sys.excepthook(*sys.exc_info())
            self.assertTrue(err.called, "excepthook 没把异常写进日志")
            self.assertIn("boom-测试用", str(err.call_args))
        finally:
            sys.excepthook = old

    def test_crash_hook_installed_after_log_setup(self):
        """excepthook 必须在 `applog.setup()` **之后**装：之前装的话日志还没有
        文件 handler，写进去也丢。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "src" / "batbar" / "agent.py").read_text(encoding="utf-8")
        body = src.split("def main():", 1)[1]
        self.assertLess(body.index("path = applog.setup("),
                        body.index("install_crash_log()"))


if __name__ == "__main__":
    unittest.main()
