"""wire 桥接协议 + state 状态机单测（纯标准库）。"""
import unittest

from batbar import wire
from batbar import hidpp as H
from batbar.state import BatteryPoller, BatteryState


class TestWire(unittest.TestCase):
    def test_roundtrip(self):
        data = wire.msg_read(2)
        self.assertTrue(data.endswith(b"\n"))
        f = wire.decode_line(data)
        self.assertEqual(f.type, "read")
        self.assertEqual(f.get("devnum"), 2)

    def test_bad_lines_never_raise(self):
        self.assertIsNone(wire.decode_line(b""))
        self.assertIsNone(wire.decode_line(b"not json"))
        self.assertIsNone(wire.decode_line(b'{"no_t":1}'))
        self.assertIsNone(wire.decode_line(b"\xff\xfe"))

    def test_png_base64_roundtrip(self):
        msg = wire.msg_icon(b"\x89PNG\r\n\x1a\nfake", "tip", 85, False)
        f = wire.decode_line(msg)
        self.assertEqual(wire.un_b64_png(f.get("png")), b"\x89PNG\r\n\x1a\nfake")
        self.assertEqual(f.get("percent"), 85)
        self.assertEqual(f.type, "icon")

    def test_hello_has_version(self):
        f = wire.decode_line(wire.msg_hello())
        self.assertEqual(f.type, "hello")
        self.assertEqual(f.get("v"), wire.PROTO_VERSION)


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
        self.assertTrue(p.request_due())
        p.mark_request_sent()
        p.poll_response(None, sleeping=True)     # 失败且 agent 判定睡眠
        self.assertTrue(p.state.sleeping)
        clock[0] += 61
        self.assertFalse(p.request_due())        # 睡眠期 5min 才试一次
        clock[0] += 240
        self.assertTrue(p.request_due())

    def test_wake_restores_active_interval(self):
        clock = [0.0]
        p = BatteryPoller(now=lambda: clock[0])
        p.poll_response(None, sleeping=True)
        self.assertTrue(p.state.sleeping)
        clock[0] += 301
        p.mark_request_sent()
        p.poll_response(self._info())
        self.assertFalse(p.state.sleeping)
        clock[0] += 61
        self.assertTrue(p.request_due())

    def test_notification_always_accepted(self):
        clock = [0.0]
        p = BatteryPoller(now=lambda: clock[0])
        p.poll_response(self._info(85))
        p.take_icon_update()
        p.on_notification(self._info(33))        # 设备主动推，无视在途/档位
        self.assertIsNotNone(p.take_icon_update())
        self.assertEqual(p.state.percent, 33)

    def test_request_inflight_blocks_due(self):
        p = BatteryPoller()
        self.assertTrue(p.request_due())
        p.mark_request_sent()
        self.assertFalse(p.request_due())
        p.note_timeout()
        # mark_request_sent 已把 last_poll_ts 推到当下 ⇒ 不立刻风暴重试
        self.assertFalse(p.request_due())

    def test_no_response_flood(self):
        """防请求风暴：在途不发第二条；持续无响应 ⇒ 状态机转入睡眠退避（300s）。"""
        clock = [0.0]
        p = BatteryPoller(now=lambda: clock[0])
        clock[0] = 60.0                       # 首次到期
        self.assertTrue(p.request_due())
        p.mark_request_sent()                 # 发出，进入在途
        clock[0] += 5
        self.assertFalse(p.request_due())     # 在途：绝不发第二条
        p.note_timeout()                      # agent 没回，超时
        self.assertFalse(p.request_due())     # 超时把 last_poll_ts 推到当下 ⇒ 不立刻重试
        # 从未成功过 + 读失败 ⇒ poller 判定休眠，退避到 300s 周期
        clock[0] += 61
        self.assertFalse(p.request_due())
        clock[0] += 300
        self.assertTrue(p.request_due())      # 睡眠周期到，再给设备一次机会

    def test_hidpp_device_present_backoff_is_active(self):
        clock = [0.0]
        p = BatteryPoller(now=lambda: clock[0])
        clock[0] = 60.0
        p.mark_request_sent()
        p.poll_response(self._info())         # 有过成功读数
        clock[0] += 5
        p.mark_request_sent()
        p.note_timeout()                      # 单次超时
        self.assertFalse(p.state.sleeping)    # 设备在线标记保持
        clock[0] += 61
        self.assertTrue(p.request_due())      # 正常 60s 节奏继续

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

    def test_tooltip_contains_percent(self):
        p = BatteryPoller()
        p.poll_response(self._info(85, charging=True))
        tip = p.state.tooltip("M3")
        self.assertIn("85%", tip)
        self.assertIn("充电中", tip)


if __name__ == "__main__":
    unittest.main()
