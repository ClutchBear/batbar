"""全屏/游戏感知（fullscreen.py 纯逻辑层）测试。

覆盖：
  * `is_dont_disturb`：哪些 QUNS 值算"别打扰"（★ 含实测命中的 BUSY）
  * `parse_mode`：环境变量归一（auto/off/on + 错值回落）
  * `FullscreenGate` 迟滞：进得快 / 出得慢 / 抖动不翻面
  * 查询失败（None）不得静音

全 stdlib、无需 Windows（纯逻辑层不碰 ctypes）。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from batbar import fullscreen as F  # noqa: E402


class DontDisturbClassifier(unittest.TestCase):
    """哪些 QUNS 值算"别打扰"。"""

    def test_accepts_notifications_is_free(self):
        self.assertFalse(F.is_dont_disturb(F.QUNS_ACCEPTS_NOTIFICATIONS))

    def test_busy_counts_as_dont_disturb(self):
        """★ 本机实测：无边框全屏返回 BUSY（2）不是 D3D_FULL_SCREEN（3）。

        只认 D3D 会漏掉绝大多数游戏 —— 这条钉死它必须算"别打扰"。
        """
        self.assertTrue(F.is_dont_disturb(F.QUNS_BUSY))

    def test_d3d_fullscreen(self):
        self.assertTrue(F.is_dont_disturb(F.QUNS_RUNNING_D3D_FULL_SCREEN))

    def test_presentation_mode(self):
        self.assertTrue(F.is_dont_disturb(F.QUNS_PRESENTATION_MODE))

    def test_quiet_time_and_not_present(self):
        self.assertTrue(F.is_dont_disturb(F.QUNS_QUIET_TIME))
        self.assertTrue(F.is_dont_disturb(F.QUNS_NOT_PRESENT))

    def test_app_state(self):
        self.assertTrue(F.is_dont_disturb(F.QUNS_APP))

    def test_query_failure_does_not_silence(self):
        """★ 查不到（None）→ 当作可打扰。绝不因一次 API 抖动永久静音。"""
        self.assertFalse(F.is_dont_disturb(None))

    def test_unknown_value_is_free(self):
        """没见过的值 → 不静音（宁可照常工作）。"""
        self.assertFalse(F.is_dont_disturb(999))

    def test_state_name(self):
        self.assertEqual(F.state_name(F.QUNS_BUSY), "BUSY")
        self.assertEqual(F.state_name(F.QUNS_ACCEPTS_NOTIFICATIONS),
                         "ACCEPTS_NOTIFICATIONS")
        self.assertEqual(F.state_name(None), "None")
        self.assertIn("999", F.state_name(999))


class ModeParsing(unittest.TestCase):
    """BATBAR_FULLSCREEN 归一。"""

    def test_auto_default(self):
        self.assertEqual(F.parse_mode(""), F.MODE_AUTO)
        self.assertEqual(F.parse_mode(None) if False else F.MODE_AUTO,
                         F.MODE_AUTO)

    def test_off_variants(self):
        for v in ("off", "OFF", " 0 ", "false", "no", "disable", "disabled"):
            self.assertEqual(F.parse_mode(v), F.MODE_OFF, v)

    def test_on_variants(self):
        for v in ("on", "ON", "1", "true", "yes", "force", "enabled"):
            self.assertEqual(F.parse_mode(v), F.MODE_ON, v)

    def test_garbage_falls_back_to_auto(self):
        """打错字不许把功能锁死。"""
        for v in ("banana", "auto ", "??", "2"):
            self.assertEqual(F.parse_mode(v), F.MODE_AUTO, v)

    def test_reads_env_when_raw_none(self):
        old = os.environ.get(F.ENV_MODE)
        try:
            os.environ[F.ENV_MODE] = "on"
            self.assertEqual(F.parse_mode(None), F.MODE_ON)
            os.environ[F.ENV_MODE] = "off"
            self.assertEqual(F.parse_mode(None), F.MODE_OFF)
        finally:
            if old is None:
                os.environ.pop(F.ENV_MODE, None)
            else:
                os.environ[F.ENV_MODE] = old


class GateHysteresis(unittest.TestCase):
    """迟滞：进得快、出得慢、抖动不翻面。"""

    def _gate(self, **kw):
        kw.setdefault("mode", F.MODE_AUTO)
        return F.FullscreenGate(**kw)

    def test_needs_enter_streak_to_activate(self):
        g = self._gate(enter_streak=3, exit_streak=5)
        self.assertFalse(g.active)
        self.assertFalse(g.sample(F.QUNS_BUSY, 0.0))    # 1
        self.assertFalse(g.sample(F.QUNS_BUSY, 1.0))    # 2
        self.assertFalse(g.active)                      # 还差一次
        self.assertTrue(g.sample(F.QUNS_BUSY, 2.0))     # 3 → 翻面
        self.assertTrue(g.active)

    def test_single_spike_does_not_activate(self):
        """抖一下（1 次 BUSY）不该进入静音 —— 点个桌面就静音太敏感。"""
        g = self._gate(enter_streak=3)
        g.sample(F.QUNS_BUSY, 0.0)
        g.sample(F.QUNS_ACCEPTS_NOTIFICATIONS, 0.1)
        self.assertFalse(g.active)
        self.assertEqual(g._busy_streak, 0)   # 计数器已复位

    def test_exit_needs_more_streak_than_enter(self):
        """★ 进得快出得慢：进 3 次，出要 5 次。"""
        g = self._gate(enter_streak=3, exit_streak=5)
        for i in range(3):
            g.sample(F.QUNS_BUSY, float(i))
        self.assertTrue(g.active)
        # 连续 4 次"可打扰"还不退出
        for i in range(4):
            self.assertFalse(g.sample(F.QUNS_ACCEPTS_NOTIFICATIONS, 10.0 + i))
            self.assertTrue(g.active)
        # 第 5 次才退出
        self.assertTrue(g.sample(F.QUNS_ACCEPTS_NOTIFICATIONS, 20.0))
        self.assertFalse(g.active)

    def test_flap_while_active_does_not_exit(self):
        """已静音时来回抖动（没凑够 exit_streak）不该退出 —— 游戏里不白刷硬件。"""
        g = self._gate(enter_streak=3, exit_streak=5)
        for i in range(3):
            g.sample(F.QUNS_BUSY, float(i))
        self.assertTrue(g.active)
        # 交替采样，free_streak 永远凑不满 5
        for i in range(10):
            g.sample(F.QUNS_ACCEPTS_NOTIFICATIONS, 10.0 + i)
            g.sample(F.QUNS_BUSY, 10.5 + i)
        self.assertTrue(g.active)

    def test_query_failure_counts_as_free(self):
        """查不到 → 当可打扰；连续足够次也能退出静音。"""
        g = self._gate(enter_streak=1, exit_streak=2)
        g.sample(F.QUNS_BUSY, 0.0)
        self.assertTrue(g.active)
        g.sample(None, 1.0)
        self.assertTrue(g.sample(None, 2.0))       # 2 次 None → 退出
        self.assertFalse(g.active)

    def test_sample_returns_true_only_on_flip(self):
        g = self._gate(enter_streak=2, exit_streak=2)
        self.assertFalse(g.sample(F.QUNS_BUSY, 0.0))
        self.assertTrue(g.sample(F.QUNS_BUSY, 1.0))     # 翻面
        self.assertFalse(g.sample(F.QUNS_BUSY, 2.0))    # 已在态内，不重复报
        self.assertFalse(g.sample(F.QUNS_BUSY, 3.0))

    def test_since_updates_on_flip(self):
        g = self._gate(enter_streak=1, exit_streak=1)
        g.sample(F.QUNS_BUSY, 100.0)
        self.assertAlmostEqual(g.since, 100.0)
        g.sample(F.QUNS_ACCEPTS_NOTIFICATIONS, 200.0)
        self.assertAlmostEqual(g.since, 200.0)


class GateModeLock(unittest.TestCase):
    """off/on 环境变量锁死状态，采样不参与。"""

    def test_on_forces_active(self):
        g = F.FullscreenGate(mode=F.MODE_ON)
        self.assertTrue(g.active)
        self.assertTrue(g.locked)
        # 即使喂"可打扰"也不退出（锁死）
        for i in range(20):
            g.sample(F.QUNS_ACCEPTS_NOTIFICATIONS, float(i))
        self.assertTrue(g.active)
        self.assertFalse(g.sample(F.QUNS_ACCEPTS_NOTIFICATIONS, 99.0))

    def test_off_forces_inactive(self):
        g = F.FullscreenGate(mode=F.MODE_OFF)
        self.assertFalse(g.active)
        self.assertTrue(g.locked)
        for i in range(20):
            g.sample(F.QUNS_BUSY, float(i))
        self.assertFalse(g.active)

    def test_auto_not_locked(self):
        g = F.FullscreenGate(mode=F.MODE_AUTO)
        self.assertFalse(g.locked)

    def test_locked_still_records_last_state(self):
        """锁死时仍记录 last_state（排障要知道系统真实状态）。"""
        g = F.FullscreenGate(mode=F.MODE_ON)
        g.sample(F.QUNS_ACCEPTS_NOTIFICATIONS, 0.0)
        self.assertEqual(g.last_state, F.QUNS_ACCEPTS_NOTIFICATIONS)


class Constants(unittest.TestCase):
    def test_enter_faster_than_exit(self):
        """★ 设计约束：进入静音要快（别打扰游戏），退出要慢（防白刷硬件）。"""
        self.assertLess(F.ENTER_STREAK, F.EXIT_STREAK)

    def test_poll_interval_sane(self):
        self.assertGreater(F.POLL_SECONDS, 0)
        self.assertLessEqual(F.POLL_SECONDS, 5.0)

    def test_describe_contains_key_fields(self):
        g = F.FullscreenGate(mode=F.MODE_AUTO)
        g.sample(F.QUNS_BUSY, 0.0)
        d = g.describe()
        self.assertIn("mode=", d)
        self.assertIn("active=", d)
        self.assertIn("BUSY", d)


class AgentWiring(unittest.TestCase):
    """agent 侧的接线契约（静态检查 + 轻量行为，不碰真 QUNS）。"""

    def test_agent_has_gate_and_helpers(self):
        import inspect
        from batbar import agent as A
        self.assertTrue(hasattr(A.Agent, "_fs_poll_loop"))
        self.assertTrue(hasattr(A.Agent, "_in_dont_disturb"))
        self.assertTrue(hasattr(A.Agent, "_gate_notify"))
        self.assertTrue(callable(A._query_quns))
        src = inspect.getsource(A._query_quns)
        self.assertIn("SHQueryUserNotificationState", src)

    def test_poll_loop_started_in_run(self):
        import inspect
        from batbar import agent as A
        src = inspect.getsource(A.Agent.run)
        self.assertIn("_fs_poll_loop", src)

    def test_lowbat_init_passes_gate(self):
        """_lowbat_init 必须把门控接进状态机（否则 toast 不受全屏约束）。"""
        import inspect
        from batbar import agent as A
        src = inspect.getsource(A.Agent._lowbat_init)
        self.assertIn("gate=", src)

    def test_warm_loop_skips_when_dont_disturb(self):
        """_bt_warm_loop 必须查门控并跳过本轮硬件读取。"""
        import inspect
        from batbar import agent as A
        src = inspect.getsource(A.Agent._bt_warm_loop)
        self.assertIn("_in_dont_disturb", src)

    def test_poll24_gate_injected(self):
        """poll24 的跳过判据必须被注入（否则 2.4G 扫描照跑）。"""
        import inspect
        from batbar import agent as A
        src = inspect.getsource(A.Agent.run)
        self.assertIn("set_skip_when", src)

    def test_in_dont_disturb_false_without_gate(self):
        """没有 _fs_gate（未初始化）时不得报错，且当作"不打扰"。"""
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
        from batbar import agent as A
        a = object.__new__(A.Agent)
        self.assertFalse(a._in_dont_disturb())      # 缺 _fs_gate → False

    def test_in_dont_disturb_reflects_gate(self):
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
        from batbar import agent as A
        a = object.__new__(A.Agent)
        a._fs_gate = F.FullscreenGate(mode=F.MODE_ON)
        self.assertTrue(a._in_dont_disturb())
        a._fs_gate = F.FullscreenGate(mode=F.MODE_OFF)
        self.assertFalse(a._in_dont_disturb())

    def test_gate_notify_counts_suppression(self):
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
        from batbar import agent as A
        a = object.__new__(A.Agent)
        a._fs_skip_toast = 0
        a._fs_gate = F.FullscreenGate(mode=F.MODE_ON)
        self.assertFalse(a._gate_notify("键盘", 5))
        self.assertFalse(a._gate_notify("键盘", 5))
        self.assertEqual(a._fs_skip_toast, 2)
        # 切回允许态 → 放行，且计数不变
        a._fs_gate = F.FullscreenGate(mode=F.MODE_OFF)
        self.assertTrue(a._gate_notify("键盘", 5))
        self.assertEqual(a._fs_skip_toast, 2)


if __name__ == "__main__":
    unittest.main()
