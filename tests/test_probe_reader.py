"""probe_query.Reader 常量防回归测试。

背景（2026-10-12 15:55 破案）：Reader.ERROR_IO_PENDING 曾被误写为 99
（真值 997 = 0x3E5）。后果链：ReadFile 返回 FALSE+997 时被当成「非 PENDING
错误」立即 return None，**从不等待迟到的应答帧** —— 迈从历轮「零回帧」实验
全部被这个笔误污染（LEOBOG 帧密集、首读常同步命中所以幸存，掩盖了缺陷）。
常量写死在类里，最容易再被顺手改错，故上锁。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import probe_query as pq  # noqa: E402


class IoPendingConstant(unittest.TestCase):
    def test_error_io_pending_is_win32_true_value(self):
        # winerror.h: ERROR_IO_PENDING 0x3E5 (997)
        self.assertEqual(pq.Reader.ERROR_IO_PENDING, 997)

    def test_other_constants_sane(self):
        self.assertEqual(pq.Reader.WAIT_TIMEOUT, 0x0102)
        self.assertEqual(pq.Reader.ERROR_OPERATION_ABORTED, 1223)
        self.assertEqual(pq.Reader.ERROR_SEM_TIMEOUT, 121)


class OfficialFrameLayout(unittest.TestCase):
    def test_checksum_tail_position(self):
        # 官方帧 = 13 4a 01 + 16×00 + 校验和（与 bundle `o=`${l} ${X3e(l)}``
        # 逐字节一致；校验和在最末位，不是第 5 字节 —— dbt 的截断变体是错的）。
        body = bytearray([0x13, 0x4A, 0x01] + [0x00] * 16)
        chk = sum(body) & 0xFF
        cmd = bytes(body) + bytes([chk])
        self.assertEqual(chk, 0x5E)
        self.assertEqual(len(cmd), 20)
        self.assertEqual(cmd[-1], 0x5E)
        self.assertEqual(cmd[4], 0x00)  # 第 5 字节必须是零（截断变体反例）


class MchoseRealFrames(unittest.TestCase):
    """2026-10-12 17:00 监听实抓的迈从真帧 —— 事件驱动推送破案样本。

    迈从 0xFF02 与 LEOBOG 同族同布局：`13 0a 01 00 04 <type> <value> …`
    （[5]=type、[6]=值、[19]=sum8(前 8 字节)）。电量 = 百分比直读，
    只在开机重连 / 插拔充电 / 灯变化 / 收到查询时推送，平时静默。
    """

    def setUp(self):
        from batbar import dongle

        self.parse = dongle.parse_frame

    def parse_raw(self, raw: bytes):
        return self.parse(raw)

    def test_battery_charging_climb(self):
        # 插充电线实测：95 → 96 → 97 递增
        for lvl, tail in ((0x5F, 0x87), (0x60, 0x88), (0x61, 0x89)):
            raw = bytes(
                [0x13, 0x0A, 0x01, 0x00, 0x04, 0x05, lvl, 0x01, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, tail]
            )
            self.assertEqual(self.parse_raw(raw), ("battery", lvl))
            self.assertEqual(sum(raw[:8]) & 0xFF, tail)

    def test_battery_full_status(self):
        # 0x11 状态字节（fullStatus bit4=1）
        raw = bytes([0x13, 0x0A, 0x01, 0x00, 0x04, 0x05, 0x64, 0x11, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x9C])
        self.assertEqual(self.parse_raw(raw), ("battery", 100))

    def test_battery_low_sample(self):
        raw = bytes([0x13, 0x0A, 0x01, 0x00, 0x04, 0x05, 0x3F, 0x11, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x77])
        self.assertEqual(self.parse_raw(raw), ("battery", 63))

    def test_backlight_and_handshake(self):
        on = bytes([0x13, 0x0A, 0x01, 0x00, 0x04, 0x02, 0x01, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x25])
        off = bytes([0x13, 0x0A, 0x01, 0x00, 0x04, 0x02, 0x00, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x24])
        hs = bytes([0x13, 0x0A, 0x01, 0x00, 0x04, 0x07, 0x00, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x29])
        self.assertEqual(self.parse_raw(on), ("backlight", 1))
        self.assertEqual(self.parse_raw(off), ("backlight", 0))
        self.assertEqual(self.parse_raw(hs), ("handshake", 0))

    def test_command_ack_is_battery(self):
        # Hub 查询的命令应答帧（13 4a total current data… sum），payload 0x5D=93。
        # 2026-10-12 poll24 落地后契约反转：应答帧正是轮询要的电量来源，
        # parse_frame 新增 4a 分支 → ("battery", 93)（此前旧契约为 None）。
        raw = bytes([0x13, 0x4A, 0x01, 0x00, 0x02, 0x5D, 0x01, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xBE])
        self.assertEqual(self.parse_raw(raw), ("battery", 93))


class Tracked24GKey(unittest.TestCase):
    """托盘跟踪源 key 判定：2.4G/有线条目必须走 "24G:<基名>" 而非 "BT:<名>"。

    背景（2026-10-12）：用户点迈从条目后跟踪源被设成 "BT:迈从键盘 (2.4G)"，
    _tracked_read 的 BT 分支查蓝牙 CIM 列表永远查不到 → 托盘图标画 "?"。
    """

    def test_menu_key_routing(self):
        base = "迈从键盘"
        self.assertEqual(base + " (2.4G)", base + "\u0020(2.4G)")
        from batbar import receiver24 as R

        # 后缀判据：base_name 能剥出基名
        self.assertEqual(R.base_name(base + R.SUFFIX), base)
        self.assertEqual(R.base_name(base), base)  # 无后缀原样返回

    def test_tracked_read_24g_branch(self):
        """24G: 跟踪源读数 = dongle.battery 优先，lastknown 兜底。"""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from batbar import agent as A
        from batbar import dongle, lastknown

        ag = A.Agent.__new__(A.Agent)  # 不跑 __init__，只测分支逻辑
        ag._tracked = "24G:测试键盘"

        calls = {"n": 0}

        def fake_battery(name):
            calls["n"] += 1
            return 95 if name == "测试键盘" else None

        orig = dongle.battery
        try:
            dongle.battery = fake_battery
            self.assertEqual(ag._tracked_read(), (95, False, "ok"))

            # dongle 无帧 → lastknown 兜底
            dongle.battery = lambda name: None
            orig_lk = lastknown.get
            lastknown.get = lambda name: (88, 12345.0) if name == "测试键盘" else None
            try:
                self.assertEqual(ag._tracked_read(), (88, False, "ok"))
                # 两者都无 → sleeping
                lastknown.get = lambda name: None
                self.assertEqual(ag._tracked_read(), (None, False, "sleeping"))
            finally:
                lastknown.get = orig_lk
        finally:
            dongle.battery = orig


if __name__ == "__main__":
    unittest.main()
