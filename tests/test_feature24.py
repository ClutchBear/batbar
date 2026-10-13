"""feature24.py 单测：样本环形缓冲、活帧转记、语义未定案的保守返回。

feature 命中帧来自本机 05AC:024F 的 0xFF04/0x0002 collection 实测：
    06 00 02 64 64 64 65 92
"""
from __future__ import annotations

import unittest

from batbar import feature24 as F

REAL = bytes.fromhex("0600026464646592")


class DecodeGuess(unittest.TestCase):
    def test_returns_none_until_semantics_settled(self):
        # 语义未定案（电压 vs 百分比），绝不冒充实测电量。
        self.assertIsNone(F.decode_guess(REAL))
        self.assertIsNone(F.decode_guess(b""))


class SampleBuffer(unittest.TestCase):
    def setUp(self):
        F._samples.clear()

    def tearDown(self):
        F._samples.clear()

    def test_poll_once_empty_targets_noop(self):
        # 未 discover（_found 里没有该名字）→ poll_once 返回空、不抛。
        self.assertEqual(F.poll_once("不存在设备"), [])

    def test_samples_capped_and_ordered(self):
        # 直接喂 _samples，验证 MAX_SAMPLES 截断逻辑（poll_once 依赖真硬件，
        # 这里只测缓冲不变量）。
        name = "迈从键盘"
        buf = F._samples.setdefault(name, [])
        for i in range(F.MAX_SAMPLES + 10):
            buf.append((float(i), REAL))
            if len(buf) > F.MAX_SAMPLES:
                del buf[:len(buf) - F.MAX_SAMPLES]
        self.assertEqual(len(buf), F.MAX_SAMPLES)
        self.assertEqual(buf[-1][0], F.MAX_SAMPLES + 9)   # 最新在尾

    def test_clear_cache_resets(self):
        F._found["迈从键盘"] = [("path", 8, 6)]
        F._samples["迈从键盘"] = [(1.0, REAL)]
        F.clear_cache()
        self.assertEqual(F.samples("迈从键盘"), [])
        self.assertNotIn("迈从键盘", F._found)


class NoAliveNote(unittest.TestCase):
    """拉取成功 **≠** 设备在线（2026-10-12 20:43 修，用户报「K87 完全关闭
    的情况下托盘还显示 2.4G」）。

    这块 feature buffer 在**接收器**上，键盘关机/休眠时读到的字节一字不变
    （实测 `06 00 02 64 64 64 65 92`），「读得到」只证明接收器插着。旧的
    「拉取成功转记活帧」会让 K87 永不判离线、挂着 lastknown 假装在线。
    """

    def test_no_alive_note_api(self):
        """转记活帧的能力已删除（避免有人再把它接回去）。"""
        from batbar import dongle
        from batbar import feature24 as F
        self.assertFalse(hasattr(F, "_note_alive"))
        self.assertFalse(hasattr(dongle, "note_frame"))

    def test_poll_once_does_not_touch_dongle_stats(self):
        """poll_once 只存样本，绝不往 dongle 存活统计里写东西。"""
        from batbar import dongle
        from batbar import feature24 as F
        dongle._stats.pop("迈从键盘", None)
        F._found["迈从键盘"] = [("Z:\\no-such-path", 8, 6)]
        F.poll_once("迈从键盘")
        self.assertNotIn("迈从键盘", dongle._stats)
        F._found.pop("迈从键盘", None)


if __name__ == "__main__":
    unittest.main()
