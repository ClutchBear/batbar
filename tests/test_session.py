"""HidppSession 单测：用 FakeTransport 回放字节脚本，验证协议编排正确性。

不需要任何硬件。Fake 按预置队列吐出原始帧；若回显 request_id 不匹配会
超时（测试里因此必须把回复做成 echo 请求 id 的形式——这正是 Solaar 的匹配逻辑）。
"""

import struct
import unittest

from batbar import hidpp as H
from batbar.session import HidppSession, TransportClosed


class ScriptedTransport:
    """队列式假 transport：write 记录帧并把回显算出来，read 吐预置回复。

    replies: 每次 write 会按序 pop 一个"回复构造器"，签名
             (feature_index, function, sw_id) -> payload_data(bytes) | "ERR:code"
    """

    def __init__(self, replies):
        self._replies = list(replies)
        self._pending: list[bytes] = []
        self.writes: list[tuple[bytes, int, bool]] = []
        self.closed = False

    def write(self, payload, device_index, long):
        self.writes.append((payload, device_index, long))
        rid = struct.unpack("!H", payload[:2])[0]
        feat_idx = (rid >> 8) & 0xFF
        fn = rid & 0xF0
        swid = rid & 0x0F
        item = self._replies.pop(0) if self._replies else None
        if item is None:
            return  # 没有预置回复 → 读超时
        if isinstance(item, str) and item.startswith("ERR:"):
            code = int(item[4:], 16)
            data = bytes([0xFF]) + struct.pack("!H", rid) + bytes([code])
        elif isinstance(item, tuple):
            data = bytes([feat_idx, fn | swid]) + item[0]
        else:
            data = bytes([feat_idx, fn | swid]) + item
        frame = bytes([H.REPORT_ID_SHORT, device_index]) + data.ljust(H.SHORT_DATA, b"\x00")
        self._pending.append(frame)

    def read(self, timeout_s):
        return self._pending.pop(0) if self._pending else None

    def close(self):
        self.closed = True


class TestSessionFindFeature(unittest.TestCase):
    def test_get_feature_index(self):
        # ROOT(idx 0) fn 0x00 回复: [index=0x05]
        t = ScriptedTransport([bytes([0x05])])
        s = HidppSession(t, device_index=0xFF)
        idx = s.find_feature(H.FEAT_UNIFIED_BATTERY)
        self.assertEqual(idx, 0x05)
        # 发出的请求：ROOT 页、function 0、参数 = 0x1004 大端
        payload, devnum, long = t.writes[0]
        rid = struct.unpack("!H", payload[:2])[0]
        self.assertEqual(rid >> 8, 0x00)
        self.assertEqual(rid & 0xF0, 0x00)
        self.assertEqual(payload[2:4], struct.pack("!H", H.FEAT_UNIFIED_BATTERY))
        self.assertEqual(devnum, 0xFF)
        self.assertFalse(long)

    def test_get_feature_error_gives_none(self):
        t = ScriptedTransport(["ERR:01"])  # unsupported
        s = HidppSession(t)
        self.assertIsNone(s.find_feature(0x9999))

    def test_feature_index_cached(self):
        t = ScriptedTransport([bytes([0x05])])
        s = HidppSession(t)
        s.find_feature(H.FEAT_UNIFIED_BATTERY)
        idx2 = s.find_feature(H.FEAT_UNIFIED_BATTERY)  # 第二次不再发帧
        self.assertEqual(idx2, 0x05)
        self.assertEqual(len(t.writes), 1)


class TestSessionReadBattery(unittest.TestCase):
    def _unified_ok(self):
        # 两次 ROOT 查询（unified 命中即停），第二次是特性页读
        return ScriptedTransport(
            [
                bytes([0x05]),  # unified idx=5
                bytes([87, 4, 0x01, 0x00]),  # 0x1004 fn0x10 数据段
            ]
        )

    def test_reads_unified_battery(self):
        t = self._unified_ok()
        s = HidppSession(t, device_index=0xFF)
        info = s.read_battery()
        self.assertEqual(info.percent, 87)
        self.assertTrue(info.charging)
        self.assertEqual(info.source, "0x1004")

    def test_unified_uses_function_0x10(self):
        t = self._unified_ok()
        s = HidppSession(t)
        s.read_battery()
        # writes[0]=ROOT.GetFeatures, writes[1]=电池读。检查第二帧的 function
        payload = t.writes[1][0]
        rid = struct.unpack("!H", payload[:2])[0]
        self.assertEqual((rid >> 8) & 0xFF, 0x05)  # 特性索引来自 GetFeatures
        self.assertEqual(rid & 0xF0, 0x10)

    def test_error_frame_on_read_returns_none(self):
        t = ScriptedTransport([bytes([0x05]), "ERR:01"])
        s = HidppSession(t)
        self.assertIsNone(s.read_battery())


class TestSessionNotification(unittest.TestCase):
    def test_notification_during_reply_wait_is_swallowed(self):
        # 先吐一条电池通知（swid bit3=0），再吐动态回显请求 swid 的匹配回复
        notif = bytes([H.REPORT_ID_SHORT, 0xFF, 0x05, 0x00, 42, 4, 0x00, 0x00])

        class TwoPhase:
            def __init__(self):
                self.frames = [notif]
                self.closed = False

            def write(self, payload, device_index, long):
                # 动态回显：[feat_idx, fn|swid] = request_id 前两字节原样
                data = payload[:2] + bytes([77, 4, 0x00, 0x00])
                frame = bytes([H.REPORT_ID_SHORT, device_index]) + data.ljust(H.SHORT_DATA, b"\x00")
                self.frames.append(frame)

            def read(self, timeout):
                return self.frames.pop(0) if self.frames else None

            def close(self):
                self.closed = True

        t = TwoPhase()
        s = HidppSession(t)
        s._battery_feat = (H.FEAT_UNIFIED_BATTERY, 0x05)
        got = []
        s.set_notification_callback(got.append)
        info = s.read_battery()
        self.assertEqual(info.percent, 77)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].percent, 42)


class TestSessionPing(unittest.TestCase):
    def test_ping_ok(self):
        # PING: ROOT 页 fn0x10, 参数 0；回复数据 [4, 2] → 4.2
        t = ScriptedTransport([bytes([4, 2])])
        s = HidppSession(t)
        self.assertEqual(s.ping(), 4.2)

    def test_ping_timeout(self):
        t = ScriptedTransport([])  # 无回复
        s = HidppSession(t)
        self.assertIsNone(s.ping())


class TestSessionReadRegister(unittest.TestCase):
    """HID++ 1.0 寄存器读（sub_id 0x81）底层原语。

    ⚠️ 寄存器 0x02「Connection State」判不了「设备关机没」（2026-10-13
    实测：本机鼠标关机后已连接数不掉），基于它的 connection_state() 判据
    已整体移除；在/离线判定改走 `read_battery()` 应不应答（两次不应答 =
    不在，见 `agent._read_with_second_chance`）。read_register 保留为
    通用底层原语，供后续寄存器探索使用。
    """

    def test_read_register_returns_data_section(self):
        t = ScriptedTransport([bytes([0x00, 0x01, 0x00])])
        s = HidppSession(t)
        self.assertEqual(s.read_register(0x00, device_index=H.DEV_RECEIVER), bytes([0x00, 0x01, 0x00]))
        payload, devnum, _long = t.writes[0]
        self.assertEqual(payload[:2], bytes([0x81, 0x00]))  # 寄存器号没被 sw_id 掩掉
        self.assertEqual(devnum, H.DEV_RECEIVER)  # 寻址接收器
        self.assertFalse(_long)

    def test_read_register_none_on_error_frame(self):
        t = ScriptedTransport(["ERR:01"])  # 寄存器不支持 → 错误帧 → None
        s = HidppSession(t)
        self.assertIsNone(s.read_register(0x02, device_index=H.DEV_RECEIVER))

    def test_read_register_none_on_timeout(self):
        t = ScriptedTransport([])  # 无回复 → None
        s = HidppSession(t)
        self.assertIsNone(s.read_register(0x02, device_index=H.DEV_RECEIVER))


class TestSessionTransportClosed(unittest.TestCase):
    def test_write_raises_propagates(self):
        class Dead(ScriptedTransport):
            def write(self, payload, device_index, long):
                raise TransportClosed()

        s = HidppSession(Dead([]))
        with self.assertRaises(TransportClosed):
            s.read_battery()


if __name__ == "__main__":
    unittest.main()
