"""hidpp 协议层单测（纯标准库，任意 Python 3.10+ 可跑）。

样本帧全部按 Solaar 源码的字节布局手工构造：
  短帧 = [0x10, devnum] + 5 字节 data
  data = [feature_index_hi | swid 高字节, function<<4 | swid 低字节, params...]
"""

import struct
import unittest

from batbar import hidpp as H


class TestEncode(unittest.TestCase):
    def test_feature_request_layout(self):
        # 特性索引 0x05、功能 0x10、sw_id 3 → request_id = 0x0513
        payload = H.encode_feature_request(0x05, 0x10, 3)
        self.assertEqual(payload, struct.pack("!H", 0x0513))

    def test_feature_request_swid_only_in_low_nibble(self):
        # 功能号占高 nibble，sw_id 永远只覆盖低 nibble（bit3 置位约定）
        payload = H.encode_feature_request(0x01, 0x00, 7)
        rid = struct.unpack("!H", payload)[0]
        self.assertEqual(rid & 0xF0, 0x00)  # function 位
        self.assertEqual(rid & 0x0F, 0x07)  # sw_id
        self.assertEqual(rid >> 8, 0x01)  # feature index 回显

    def test_feature_request_with_params(self):
        payload = H.encode_feature_request(0x00, 0x00, 1, struct.pack("!H", H.FEAT_UNIFIED_BATTERY))
        self.assertEqual(payload, struct.pack("!HBB", 0x0001, 0x10, 0x04))

    def test_swid_range(self):
        with self.assertRaises(AssertionError):
            H.encode_feature_request(0x01, 0x00, 0)
        with self.assertRaises(AssertionError):
            H.encode_feature_request(0x01, 0x00, 8)


class TestWriteFrame(unittest.TestCase):
    def test_short_frame_total_7_bytes(self):
        payload = H.encode_feature_request(0x05, 0x00, 3)
        frame = H.build_write_frame(H.DEV_RECEIVER, payload)
        self.assertEqual(len(frame), 7)  # 1 rid + 1 devnum + 5 data
        self.assertEqual(frame[0], H.REPORT_ID_SHORT)
        self.assertEqual(frame[1], H.DEV_RECEIVER)

    def test_long_frame_pads_to_20(self):
        payload = H.encode_feature_request(0x05, 0x30, 2, b"\x11" * 16)
        frame = H.build_write_frame(0x01, payload, long_message=True)
        self.assertEqual(len(frame), 20)
        self.assertEqual(frame[0], H.REPORT_ID_LONG)


class TestParseInput(unittest.TestCase):
    def _reply(self, feat_idx, function, data, swid=3):
        rid = (feat_idx << 8 | function) & 0xFFF0 | swid
        payload = struct.pack("!H", rid) + bytes(data)
        return bytes([H.REPORT_ID_SHORT, H.DEV_RECEIVER]) + payload.ljust(H.SHORT_DATA, b"\x00")

    def test_parse_with_report_id(self):
        raw = self._reply(0x05, 0x00, [72, 0, 1])
        parsed = H.parse_input_report(raw)
        self.assertIsNotNone(parsed)
        rid, devnum, payload = parsed
        self.assertEqual(rid, H.REPORT_ID_SHORT)
        self.assertEqual(devnum, H.DEV_RECEIVER)
        self.assertEqual(payload[:2], raw[2:4])

    def test_parse_without_report_id(self):
        # hidapi 某些版本 read() 不回带 report id：devnum 首字节
        raw = bytes([H.DEV_RECEIVER]) + struct.pack("!H", 0x0500 | 3) + b"\x48\x00\x01"
        parsed = H.parse_input_report(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[2][:2], raw[1:3])

    def test_error_frames(self):
        # Solaar base.py:732/747：错误帧首字节即标识（0x8F=HID++1.0 / 0xFF=HID++2.0），
        # 回显 request_id 在 payload[1:3]，错误码在 payload[3]
        self.assertEqual(H.frame_error(bytes([0x8F, 0x05, 0x13, 0x01])), 0x01)  # HID++1.0 错误
        self.assertEqual(H.frame_error(bytes([0xFF, 0x05, 0x13, 0x03])), 0x03)  # HID++2.0 错误
        self.assertIsNone(H.frame_error(bytes([0x05, 0x13, 0x48, 0x00])))  # 正常回复
        self.assertIsNone(H.frame_error(b""))

    def test_notification_detection(self):
        # sw_id ∈ 0x08..0x0F（bit3 恒置位）；请求回复回显该字节，通知帧不置 bit3
        self.assertTrue(H.is_notification(bytes([0x05, 0x04])))  # 通知：低 nibble < 8
        self.assertFalse(H.is_notification(bytes([0x05, 0x1B])))  # 请求回复（function=0x10, sw_id=0x0B）
        self.assertFalse(H.is_notification(bytes([0x05, 0x0B])))  # 请求回复（function=0x00, sw_id=0x0B）
        self.assertFalse(H.is_notification(b""))

    def test_frame_accessors(self):
        p = bytes([0x05, 0x13, 0x48])
        self.assertEqual(H.frame_feature_index(p), 0x05)
        self.assertEqual(H.frame_function(p), 0x10)


class TestBatteryParsers(unittest.TestCase):
    def test_unified_battery_exact(self):
        # Solaar: [discharge%, level_byte, status, ignore]
        info = H.parse_unified_battery(bytes([85, 4, 0x01, 0x00]))
        self.assertEqual(info.percent, 85)
        self.assertTrue(info.charging)
        self.assertEqual(info.approx, "good")
        self.assertEqual(info.source, "0x1004")

    def test_unified_battery_zero_discharge_falls_back_to_approx(self):
        info = H.parse_unified_battery(bytes([0, 8, 0x03, 0x00]))
        self.assertEqual(info.percent, 100)  # FULL 档 → 100
        self.assertTrue(info.charging)

    def test_unified_battery_critical_and_empty(self):
        self.assertEqual(H.parse_unified_battery(bytes([0, 1, 0x00, 0])).percent, 5)
        self.assertEqual(H.parse_unified_battery(bytes([0, 0, 0x00, 0])).percent, 0)

    def test_battery_status(self):
        info = H.parse_battery_status(bytes([20, 10, 0x00]))
        self.assertEqual(info.percent, 20)
        self.assertFalse(info.charging)

    def test_battery_status_zero_level_unknown(self):
        info = H.parse_battery_status(bytes([0, 0, 0x00]))
        self.assertIsNone(info.percent)

    def test_battery_voltage_interpolation(self):
        # 3989mV = 表上 80%；3955 在 70..80 折点之间
        info = H.parse_battery_voltage(struct.pack(">HB", 3989, 0x00))
        self.assertEqual(info.percent, 80)
        info2 = H.parse_battery_voltage(struct.pack(">HB", 3955, 0x80))
        self.assertTrue(info2.charging)
        self.assertTrue(70 <= info2.percent <= 80)

    def test_voltage_clamps(self):
        self.assertEqual(H.estimate_voltage_percent(5000), 100)
        self.assertEqual(H.estimate_voltage_percent(3000), 0)

    def test_adc_invalid_flag(self):
        self.assertIsNone(H.parse_adc_measurement(struct.pack(">HB", 3800, 0x00)))
        info = H.parse_adc_measurement(struct.pack(">HB", 3800, 0x03))
        self.assertTrue(info.charging)

    def test_parse_notification_battery_guards(self):
        # 非电池特性页的通知 → None
        payload = bytes([0x09, 0x00, 85, 4, 1, 0])
        self.assertIsNone(H.parse_notification_battery(payload, 0x05, H.FEAT_UNIFIED_BATTERY))
        # 命中特性索引 + function=0 → 解析
        info = H.parse_notification_battery(
            bytes([0x05, 0x00, 85, 4, 0x01, 0x00]), 0x05, H.FEAT_UNIFIED_BATTERY
        )
        self.assertEqual(info.percent, 85)

    def test_percent_property_mapping(self):
        self.assertEqual(H.BatteryInfo(None, "low", False, None, "0x1000").percent, 20)
        self.assertEqual(H.BatteryInfo(None, "empty", False, None, "0x1000").percent, 0)
        self.assertEqual(H.BatteryInfo(37, None, False, None, "0x1001").percent, 37)


if __name__ == "__main__":
    unittest.main()
