# -*- coding: utf-8 -*-
"""有线（USB 数据线）通道：应答解析 + 状态缓存（2026-10-12 新增）。

真实样本取自本机实测（MCHOSE K87 拨 2.4G 档 + 插线，18:5x）：
  发 06 87 00 00 01 00 02 00  →  回 06 87 00 00 01 00 02 00 5a 10 …
                                                  [8]=0x5a=90%  [9]=0x10=充电中
同刻 2.4G 通道也报 90%，两条链路互证。
"""

from __future__ import annotations

import unittest

from batbar import wired24 as W

# 实测应答（截前 20 字节）
REAL_ACK = bytes.fromhex("06 87 00 00 01 00 02 00 5a 10 00 00 00 00 00 00 00 00 00 00")


class ParseWired(unittest.TestCase):
    def test_real_sample(self):
        self.assertEqual(W.parse_wired(REAL_ACK), (90, True))

    def test_charging_flag_zero(self):
        """充电标志为 0 → 未充电（电量仍是 payload[0]）。"""
        raw = bytearray(REAL_ACK)
        raw[9] = 0x00
        self.assertEqual(W.parse_wired(bytes(raw)), (90, False))

    def test_full_flag_low_nibble(self):
        """低 4 位=充满，同样算「插着线」（图标 ⚡ + 低电豁免）。"""
        raw = bytearray(REAL_ACK)
        raw[9] = 0x01
        self.assertEqual(W.parse_wired(bytes(raw)), (90, True))

    def test_bad_header_rejected(self):
        raw = bytearray(REAL_ACK)
        raw[1] = 0x4A  # 不是 87 应答
        self.assertIsNone(W.parse_wired(bytes(raw)))

    def test_out_of_range_rejected(self):
        raw = bytearray(REAL_ACK)
        raw[8] = 0xFF  # 255% 显然不是电量
        self.assertIsNone(W.parse_wired(bytes(raw)))

    def test_short_frame_rejected(self):
        self.assertIsNone(W.parse_wired(b"\x06\x87\x00"))


class State(unittest.TestCase):
    def setUp(self):
        W.reset()

    def test_plugged_and_reading(self):
        with W._lock:
            W._state["键盘 迈从 K87"] = {"path": "x", "featlen": 520, "pct": 90, "charging": True, "ts": 0.0}
        self.assertTrue(W.plugged("键盘 迈从 K87"))
        self.assertEqual(W.battery("键盘 迈从 K87"), 90)
        self.assertTrue(W.charging("键盘 迈从 K87"))

    def test_absent_device(self):
        self.assertFalse(W.plugged("键盘 迈从 K87"))
        self.assertIsNone(W.battery("键盘 迈从 K87"))
        self.assertFalse(W.charging("键盘 迈从 K87"))

    def test_zero_while_charging_is_not_a_reading(self):
        """关机档 + 数据线：主控不上电，电量计固定回 0（探针实测 `…00 10`）。
        这不是真实电量，battery() 必须转 None → 菜单显示 "?"，不误导成 0%。"""
        with W._lock:
            W._state["键盘 迈从 K87"] = {"path": "x", "featlen": 520, "pct": 0, "charging": True, "ts": 0.0}
        self.assertIsNone(W.battery("键盘 迈从 K87"))
        self.assertTrue(W.plugged("键盘 迈从 K87"))  # 仍在充电，⚡ 照给

    def test_zero_not_charging_is_real(self):
        """0% 但没在充电（真耗干了）：是真实读数，原样返回 0。"""
        with W._lock:
            W._state["键盘 迈从 K87"] = {"path": "x", "featlen": 520, "pct": 0, "charging": False, "ts": 0.0}
        self.assertEqual(W.battery("键盘 迈从 K87"), 0)

    def test_read_failed_keeps_plugged(self):
        """插着但这一枪没读到：plugged 仍为真、电量 None（不冒充读数）。"""
        with W._lock:
            W._state["键盘 迈从 K87"] = {
                "path": "x",
                "featlen": 520,
                "pct": None,
                "charging": False,
                "ts": 0.0,
            }
        self.assertTrue(W.plugged("键盘 迈从 K87"))
        self.assertIsNone(W.battery("键盘 迈从 K87"))


class SharedArgtypes(unittest.TestCase):
    """跨模块共享 hid/setupapi DLL 时，argtypes 不得绑定模块私有结构体。

    踩坑记录（2026-10-12）：wired24 曾用 `POINTER(自己的 _ATTRS)` 声明
    `HidD_GetAttributes`，把 receiver24 的同类声明覆盖掉 → receiver24 调用时
    ctypes 类型校验失败抛 ArgumentError → 被上层 try/except 吞掉 →
    **2.4G 条目整条从托盘菜单消失**（用户实测：K87 不显示）。
    """

    def _skip_nonwin(self):
        import sys

        if not sys.platform.startswith("win"):
            self.skipTest("Windows only")

    def test_declare_then_receiver_enum_still_works(self):
        """关键回归：wired24 声明签名后，receiver24 的枚举仍必须能跑。"""
        self._skip_nonwin()
        import ctypes
        from batbar import receiver24 as R
        from batbar import wired24 as W

        W._declare(ctypes.windll.setupapi, ctypes.windll.hid, ctypes.windll.kernel32)
        R.enumerate_receivers()  # 修前这里抛 ArgumentError

    def test_shared_fn_argtypes_are_typeless(self):
        """共享函数的指针参数必须是无类型的 c_void_p。"""
        self._skip_nonwin()
        import ctypes
        from batbar import wired24 as W

        hid = ctypes.windll.hid
        W._declare(ctypes.windll.setupapi, hid, ctypes.windll.kernel32)
        for fn in ("HidD_GetAttributes", "HidP_GetCaps", "HidD_GetHidGuid"):
            for arg in getattr(hid, fn).argtypes or ():
                self.assertIs(arg, ctypes.c_void_p, f"{fn} 的指针参数绑了具体结构体，会覆盖其它模块的声明")


class LeobogWired(unittest.TestCase):
    """LEOBOG 有线通道（2026-10-12 22:10 探针实测）：插线后枚举成独立
    VID/PID `258A:010C`（与它蓝牙/接收器形态 3554:FA0x 完全不同），带
    520B feature 接口，吃迈从同款 SinoWealth `06 87` 协议。

    实测应答 `06 87 00 00 01 00 02 00 64 10` → (100, True)。
    """

    # 实测 LEOBOG 有线应答（截前 20 字节）
    LEOBOG_ACK = bytes.fromhex("06 87 00 00 01 00 02 00 64 10 00 00 00 00 00 00 00 00 00 00")

    def test_parse_leobog_sample(self):
        self.assertEqual(W.parse_wired(self.LEOBOG_ACK), (100, True))

    def test_wired_target_registered(self):
        """258A:010C 必须在 WIRED_TARGETS 里，且映射到 LEOBOG 友好名。"""
        from batbar.dongle import TARGETS, WIRED_TARGETS

        self.assertEqual(WIRED_TARGETS.get((0x258A, 0x010C)), "键盘 LEOBOG Hi98C Pro")
        # 它不能同时在 TARGETS（2.4G 接收器表）里——那是 3554:FA09
        self.assertNotIn((0x258A, 0x010C), TARGETS)

    def test_name_consistent_with_dongle_target(self):
        """WIRED_TARGETS 的友好名必须与 TARGETS 里 LEOBOG 那条**逐字一致**，
        否则 plugged()/wired_bases() 与 dongle 帧统计对不上 key，
        插线的 LEOBOG 会被当成两台设备。"""
        from batbar.dongle import TARGETS, WIRED_TARGETS

        leobog_24g = TARGETS[(0x3554, 0xFA09)]
        leobog_wired = WIRED_TARGETS[(0x258A, 0x010C)]
        self.assertEqual(leobog_24g, leobog_wired)


if __name__ == "__main__":
    unittest.main()
