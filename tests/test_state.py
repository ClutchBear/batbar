"""state 状态机单测（纯标准库）。"""
import unittest

from batbar import hidpp as H
from batbar.state import BatteryPoller, BatteryState


class TestState(unittest.TestCase):
    def _info(self, pct=85, charging=False, source="0x1004"):
        return H.BatteryInfo(pct, None, charging, None, source)

    def test_first_update_marks_dirty(self):
        p = BatteryPoller()
        p.poll_response(self._info())
        st = p.take_icon_update()
        self.assertIsNotNone(st)
        self.assertIsNone(p.take_icon_update())  # 清完后不重复

    def test_small_jitter_not_dirty(self):
        # 同 5% 档位、同充电态 → 不重绘（省电关键）
        p = BatteryPoller()
        p.poll_response(self._info(85))
        p.take_icon_update()
        p.poll_response(self._info(86))
        self.assertIsNone(p.take_icon_update())

    def test_cross_bucket_is_dirty(self):
        p = BatteryPoller()
        p.poll_response(self._info(85))
        p.take_icon_update()
        p.poll_response(self._info(80))          # 跨 5% 档
        self.assertIsNotNone(p.take_icon_update())

    def test_charging_toggle_is_dirty(self):
        p = BatteryPoller()
        p.poll_response(self._info(85, charging=False))
        p.take_icon_update()
        p.poll_response(self._info(85, charging=True))
        self.assertIsNotNone(p.take_icon_update())

    def test_sleeping_backoff(self):
        clock = [0.0]
        p = BatteryPoller(now=lambda: clock[0])
        p.poll_response(self._info())            # 一次正常读数
        p.take_icon_update()
        clock[0] += 61
        self.assertTrue(p.due())
        p.poll_response(None, sleeping=True)     # 失败且 agent 判定睡眠
        self.assertTrue(p.state.sleeping)
        clock[0] += 61
        self.assertFalse(p.due())                # 睡眠期 5min 才试一次
        clock[0] += 240
        self.assertTrue(p.due())

    def test_wake_restores_active_interval(self):
        clock = [0.0]
        p = BatteryPoller(now=lambda: clock[0])
        p.poll_response(None, sleeping=True)
        self.assertTrue(p.state.sleeping)
        clock[0] += 301
        p.poll_response(self._info())            # 醒来后的成功读数
        self.assertFalse(p.state.sleeping)
        clock[0] += 61
        self.assertTrue(p.due())

    def test_notification_always_accepted(self):
        clock = [0.0]
        p = BatteryPoller(now=lambda: clock[0])
        p.poll_response(self._info(85))
        p.take_icon_update()
        p.on_notification(self._info(33))        # 设备主动推，无视在途/档位
        self.assertIsNotNone(p.take_icon_update())
        self.assertEqual(p.state.percent, 33)

    def test_hidpp_device_present_backoff_is_active(self):
        """有过成功读数后，单次读失败**不**判定睡眠 —— 设备在线标记保持，
        仍走 60s 活跃节奏（而不是掉进 5min 退避）。"""
        clock = [0.0]
        p = BatteryPoller(now=lambda: clock[0])
        clock[0] = 60.0
        p.poll_response(self._info())         # 有过成功读数
        clock[0] += 5
        p.poll_response(None)                 # 单次读失败（agent 未断言睡眠）
        self.assertFalse(p.state.sleeping)    # 设备在线标记保持
        clock[0] += 61
        self.assertTrue(p.due())              # 正常 60s 节奏继续

    def test_never_succeeded_failure_means_sleep(self):
        """反面：从未成功读过 + 本次读失败 ⇒ 判定睡眠退避到 5min。

        与上面那条成对，覆盖 `poll_response` 里「`info is None` 才置 sleeping」的分支。
        """
        clock = [0.0]
        p = BatteryPoller(now=lambda: clock[0])
        p.poll_response(None)                 # 首次就读失败，agent 未断言睡眠
        self.assertTrue(p.state.sleeping)
        clock[0] += 61
        self.assertFalse(p.due())             # 已退避到 5min
        clock[0] += 240
        self.assertTrue(p.due())

    def test_bt_fallback_only_when_hidpp_dead(self):
        p = BatteryPoller()
        p.poll_response(self._info(85))          # HID++ 活着
        p.on_bt_battery(40)
        self.assertEqual(p.state.percent, 85)    # 蓝牙值被忽略
        clock = [0.0]
        p2 = BatteryPoller(now=lambda: clock[0])
        p2.poll_response(None, sleeping=True)    # HID++ 睡
        p2.on_bt_battery(40)
        self.assertEqual(p2.state.percent, 40)
        self.assertEqual(p2.state.info.source, "bluetooth")

    def test_async_rpc_mode_stays_deleted(self):
        """异步 RPC 三件套不许复活（可从 git 历史里捞回，故需守门）。

        `request_due()` / `mark_request_sent()` / `note_timeout()` 与在途标记
        `_awaiting` 是给旧 controller 异步回灌用的。v0.1.4 删掉桥接后，主路径
        `agent.py` 走的是同步 `due()` + `poll_once()` —— 三件套回来就是
        「只剩定义、唯一调用方是单测」的死代码，且会被自己的测试保护住。
        """
        for name in ("request_due", "mark_request_sent", "note_timeout", "_awaiting"):
            self.assertFalse(hasattr(BatteryPoller, name),
                             "BatteryPoller.%s 又回来了 —— 异步 RPC 模式已随旧桥接删除" % name)

    def test_tooltip_contains_percent(self):
        p = BatteryPoller()
        p.poll_response(self._info(85, charging=True))
        tip = p.state.tooltip("M3")
        self.assertIn("85%", tip)
        self.assertIn("充电中", tip)


if __name__ == "__main__":
    unittest.main()
