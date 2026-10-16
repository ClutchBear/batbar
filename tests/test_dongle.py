"""dongle.py 单测：帧解析用**本机真实抓到的字节**，离线判定覆盖三重保险。"""

from __future__ import annotations

import os
import time
import unittest

from batbar import dongle


def hexb(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


class ParseFrame(unittest.TestCase):
    """下面每一帧都来自本机 VID_3554&PID_FA09 接收器的实测抓包。"""

    def test_battery_frames(self):
        # 02:23:38 键盘活动时连发的电量帧，92/94/93 抖动（无滤波 ADC）
        for frame, pct in (
            ("13 0a 01 00 04 05 5c 01 00 00 00 00 00 00 00 00 00 00 00 84", 92),
            ("13 0a 01 00 04 05 5e 01 00 00 00 00 00 00 00 00 00 00 00 86", 94),
            ("13 0a 01 00 04 05 5d 01 00 00 00 00 00 00 00 00 00 00 00 85", 93),
        ):
            self.assertEqual(dongle.parse_frame(hexb(frame)), ("battery", pct))

    def test_backlight_frames(self):
        # 用户目击：02:47:59 灯灭的瞬间来的就是 02 00
        self.assertEqual(
            dongle.parse_frame(hexb("13 0a 01 00 04 02 00 00 00 00 00 00 00 00 00 00 00 00 00 24")),
            ("backlight", 0),
        )
        self.assertEqual(
            dongle.parse_frame(hexb("13 0a 01 00 04 02 01 00 00 00 00 00 00 00 00 00 00 00 00 25")),
            ("backlight", 1),
        )

    def test_handshake_frame(self):
        self.assertEqual(
            dongle.parse_frame(hexb("13 0a 01 00 04 07 00 00 00 00 00 00 00 00 00 00 00 00 00 29")),
            ("handshake", 0),
        )

    def test_bad_checksum_rejected(self):
        # 把 92% 那帧的校验位改错，必须拒收（这是过滤通道噪声的关键）
        bad = bytearray(hexb("13 0a 01 00 04 05 5c 01 00 00 00 00 00 00 00 00 00 00 00 84"))
        bad[-1] ^= 0xFF
        self.assertIsNone(dongle.parse_frame(bytes(bad)))

    def test_wrong_magic_rejected(self):
        # 标准 HID 报文（键盘 boot 协议）混进来时必须被忽略
        self.assertIsNone(dongle.parse_frame(bytes(20)))
        self.assertIsNone(
            dongle.parse_frame(hexb("01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"))
        )

    def test_short_frame_rejected(self):
        self.assertIsNone(dongle.parse_frame(hexb("13 0a 01 00 04 05 5c")))

    def test_unknown_type_passes_through(self):
        # chk = 13+0a+01+00+04+09+11+00 = 3c
        good = dongle.parse_frame(hexb("13 0a 01 00 04 09 11 00 00 00 00 00 00 00 00 00 00 00 00 3c"))
        self.assertEqual(good, ("other", 0x11))

    def test_battery_out_of_range_rejected(self):
        # A=200 不是合法百分比（sum8 已按 0xc8 重算）
        frame = "13 0a 01 00 04 05 c8 01 00 00 00 00 00 00 00 00 00 00 00 f0"
        self.assertIsNone(dongle.parse_frame(hexb(frame)))


class OfflineLogic(unittest.TestCase):
    """扫描制离线判定（2026-10-12 用户拍板，观察窗口已删除）。

    判据：①休眠帧立即不在 ②最近一次扫描没应答 → 不在 ③还没扫过 → 先按
    不在 ④扫描之后又来帧（被敲醒）→ 立即复活，不等下一轮扫描。
    """

    def setUp(self):
        dongle._stats.clear()
        from batbar import poll24

        poll24.reset()

    def tearDown(self):
        dongle._stats.clear()
        from batbar import poll24

        poll24.reset()

    def _seed(self, name: str, frame_sec: float | None, sleeping: bool = False):
        now = time.time()
        dongle._stats[name] = {
            "battery": 93 if frame_sec is not None else None,
            "battery_ts": now - frame_sec if frame_sec is not None else None,
            "frame_ts": now - frame_sec if frame_sec is not None else None,
            "sleeping": sleeping,
            "sleep_ts": (now - 7) if sleeping else None,
        }

    def _scan_result(self, name: str, online: bool, *, ago: float = 3.0):
        """伪造「最近一轮扫描」的结论（扫描时刻 = now - ago）。"""
        from batbar import poll24

        with poll24._lock:
            poll24._online[name] = online
            poll24._scanned_at[name] = time.time() - ago

    def test_no_scan_yet_means_offline(self):
        # 还没扫过 → 先按不在（启动 ~2s 内首轮扫描就出结论）
        self._seed("LEOBOG 键盘", frame_sec=None)
        self.assertTrue(dongle.is_offline("LEOBOG 键盘"))

    def test_scan_said_online(self):
        self._seed("LEOBOG 键盘", frame_sec=2)
        self._scan_result("LEOBOG 键盘", True)
        self.assertFalse(dongle.is_offline("LEOBOG 键盘"))

    def test_scan_said_offline(self):
        # 扫描判不在（休眠中问不出应答）→ 即使早前有过帧也隐藏
        self._seed("LEOBOG 键盘", frame_sec=300)
        self._scan_result("LEOBOG 键盘", False)
        self.assertTrue(dongle.is_offline("LEOBOG 键盘"))

    def test_frame_after_negative_scan_revives(self):
        # 扫描判不在，随后键盘被敲醒推来一帧 → 立即复活，不等下一轮扫描
        self._seed("LEOBOG 键盘", frame_sec=0.5)
        self._scan_result("LEOBOG 键盘", False, ago=30.0)
        self.assertFalse(dongle.is_offline("LEOBOG 键盘"))

    def test_sleep_frame_wins_over_positive_scan(self):
        # 扫描时还醒着，扫完发了休眠帧 → 立即隐藏（键盘亲口说的）
        self._seed("LEOBOG 键盘", frame_sec=1, sleeping=True)
        self._scan_result("LEOBOG 键盘", True, ago=3.0)
        self.assertTrue(dongle.is_offline("LEOBOG 键盘"))

    def test_unknown_device_offline(self):
        # 没登记也没扫过 → 按不在（旧行为是 False，但条目名来自接收器枚举，
        # 真正决定显示的是 dongle.battery；保守判不在更符合扫描制）
        self.assertTrue(dongle.is_offline("没见过的设备"))

    def test_battery_age_none_when_no_frame(self):
        self.assertIsNone(dongle.battery_age("LEOBOG 键盘"))

    def test_battery_age_positive(self):
        self._seed("LEOBOG 键盘", frame_sec=12)
        age = dongle.battery_age("LEOBOG 键盘")
        assert age is not None
        self.assertGreaterEqual(age, 11.0)

    def test_last_frame_ts_matches_seed(self):
        self._seed("LEOBOG 键盘", frame_sec=5)
        ts = dongle.last_frame_ts("LEOBOG 键盘")
        assert ts is not None
        self.assertAlmostEqual(ts, time.time() - 5, delta=1.0)


class SleepState(unittest.TestCase):
    """休眠与关机同权（2026-10-12 用户决策）：键盘一睡条目就消失。

    依据 keybeat 对照实验：驱动休眠设 30s 时，停手后 26.1s 收到
    `type=02 A=00` —— 帧是键盘亲口的「我睡了」，收到即隐藏，不等扫描。
    """

    def setUp(self):
        dongle._stats.clear()
        from batbar import poll24

        poll24.reset()

    def tearDown(self):
        dongle._stats.clear()
        from batbar import poll24

        poll24.reset()

    def _seed(self, name: str, frame_sec: float | None, sleeping: bool = False):
        now = time.time()
        dongle._stats[name] = {
            "battery": 93 if frame_sec is not None else None,
            "battery_ts": now - frame_sec if frame_sec is not None else None,
            "frame_ts": now - frame_sec if frame_sec is not None else None,
            "sleeping": sleeping,
            "sleep_ts": (now - 7) if sleeping else None,
        }

    def _scan_online(self, name: str):
        from batbar import poll24

        with poll24._lock:
            poll24._online[name] = True
            poll24._scanned_at[name] = time.time() - 30.0

    def test_sleep_frame_marks_sleeping(self):
        self._seed("LEOBOG 键盘", frame_sec=10, sleeping=True)
        self.assertTrue(dongle.is_sleeping("LEOBOG 键盘"))

    def test_awake_after_activity(self):
        self._seed("LEOBOG 键盘", frame_sec=2, sleeping=False)
        self.assertFalse(dongle.is_sleeping("LEOBOG 键盘"))

    def test_sleeping_means_offline_immediately(self):
        # 休眠帧 = 键盘亲口说「我睡了」→ 立即算不在，哪怕扫描刚判过在线
        self._seed("LEOBOG 键盘", frame_sec=7, sleeping=True)
        self._scan_online("LEOBOG 键盘")
        self.assertTrue(dongle.is_offline("LEOBOG 键盘"))

    def test_waking_frame_revives_entry(self):
        # 扫描判在线且当前醒着 → 显示
        self._seed("LEOBOG 键盘", frame_sec=1, sleeping=False)
        self._scan_online("LEOBOG 键盘")
        self.assertFalse(dongle.is_offline("LEOBOG 键盘"))

    def test_sleep_age_grows(self):
        self._seed("LEOBOG 键盘", frame_sec=7, sleeping=True)
        age = dongle.sleep_age("LEOBOG 键盘")
        assert age is not None
        self.assertGreaterEqual(age, 6.0)

    def test_sleep_age_none_when_awake(self):
        self._seed("LEOBOG 键盘", frame_sec=2)
        self.assertIsNone(dongle.sleep_age("LEOBOG 键盘"))

    def test_unknown_device_not_sleeping(self):
        self.assertFalse(dongle.is_sleeping("没见过的设备"))


class Targets(unittest.TestCase):
    def test_map_is_shared_source(self):
        from batbar import receiver24

        self.assertIs(receiver24.KNOWN_RECEIVERS, dongle.TARGETS)
        self.assertEqual(dongle.TARGETS[(0x3554, 0xFA09)], "键盘 LEOBOG Hi98C Pro")

    def test_mchose_k87_receiver_registered(self):
        """迈从 K87 的 2.4G 接收器（05AC:024F，与其 BLE 节点同 VID/PID）。"""
        self.assertEqual(dongle.TARGETS[(0x05AC, 0x024F)], "键盘 迈从 K87")

    def test_names_are_unique(self):
        """两个目标必须是不同友好名 —— _stats/_record 按 name 分 key，
        重名会让两颗键盘的状态互相覆盖。"""
        names = list(dongle.TARGETS.values())
        self.assertEqual(len(names), len(set(names)))


class WatchScheduling(unittest.TestCase):
    """_watch 调度线程：每目标一个读线程；已有活跃目标的多余句柄必须关掉。

    单读者原则的粒度是「每个 collection」——调度线程每轮都重新枚举，
    对已有活跃线程在听的目标，新拿到的句柄是同一 collection 的第二个
    句柄，留着就互抢帧（实测两边都收不全），必须立刻 CloseHandle。
    """

    def setUp(self):
        dongle._stats.clear()

    def _run(self, open_result, rounds: int = 3):
        """桩掉 _open_targets/_watch_one/threading.Thread/_stop/kernel32，
        同步跑 rounds 轮调度。返回 (起线程记录, 关句柄记录)。"""
        import ctypes
        import threading
        import types

        started: list[tuple[int, str]] = []
        closed: list[int] = []

        class _FakeThread:
            def is_alive(self):
                return True  # 「已有人在听」

            def start(self):
                pass  # target 已在构造时同步跑过

        class _Stop:
            def __init__(self, limit):
                self.rounds = 0
                self.limit = limit

            def is_set(self):
                return self.rounds >= self.limit

            def wait(self, _t):
                self.rounds += 1
                return True

            def set(self):
                pass

        def fake_thread(target=None, args=(), name=None, daemon=None):
            target(*args)  # 同步执行，免线程竞态
            return _FakeThread()

        saved = (dongle._open_targets, dongle._watch_one, dongle._stop, threading.Thread, ctypes.windll)
        dongle._open_targets = lambda: open_result
        dongle._watch_one = lambda h, n: started.append((h, n))
        dongle._stop = _Stop(rounds)
        threading.Thread = fake_thread
        ctypes.windll = types.SimpleNamespace(
            kernel32=types.SimpleNamespace(CloseHandle=lambda h: closed.append(h))
        )
        try:
            dongle._watch()
        finally:
            (dongle._open_targets, dongle._watch_one, dongle._stop, threading.Thread, ctypes.windll) = saved
        return started, closed

    def test_each_target_gets_one_thread(self):
        started, _ = self._run([(0xAAA1, "LEOBOG 键盘"), (0xAAA2, "迈从键盘")])
        self.assertEqual(sorted(n for _h, n in started), ["LEOBOG 键盘", "迈从键盘"])

    def test_existing_target_handle_closed_not_reopened(self):
        # 枚举结果恒定跑 3 轮：只准起一次线程，后两轮必须把多余句柄关掉
        started, closed = self._run([(0xAAA1, "LEOBOG 键盘")], rounds=3)
        self.assertEqual(len(started), 1)
        self.assertEqual(closed, [0xAAA1, 0xAAA1])


if __name__ == "__main__":
    unittest.main()
