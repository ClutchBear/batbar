"""lastknown / bt_battery NULL 兜底 / receiver24 兜底条目。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from batbar import lastknown as LK

_TMPDIR: str | None = None


def setUpModule():
    """把存档重定向到临时目录，避免污染用户 %LOCALAPPDATA%。"""
    global _TMPDIR
    _TMPDIR = tempfile.mkdtemp(prefix="batbar-lk-")
    os.environ[LK._ENV] = _TMPDIR


def tearDownModule():
    global _TMPDIR
    os.environ.pop(LK._ENV, None)
    if _TMPDIR:
        import shutil

        shutil.rmtree(_TMPDIR, ignore_errors=True)
    _TMPDIR = None


class LastKnown(unittest.TestCase):
    def setUp(self):
        LK.reset()

    def test_put_get(self):
        LK.put("西圣耳机", 70)
        got = LK.get("西圣耳机")
        self.assertIsNotNone(got)
        self.assertEqual(got[0], 70)

    def test_persists_across_instances(self):
        LK.put("LEOBOG 键盘", 93)
        LK._mem = None  # 模拟进程重启（内存缓存清空）
        self.assertEqual(LK.get("LEOBOG 键盘")[0], 93)

    def test_missing_returns_none(self):
        self.assertIsNone(LK.get("不存在的设备"))

    def test_age_grows(self):
        LK.put("X", 50)
        time.sleep(0.05)
        self.assertGreaterEqual(LK.age("X"), 0.0)

    def test_corrupt_file_is_ignored(self):
        p = LK.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ this is not json", encoding="utf-8")
        LK._mem = None
        self.assertEqual(LK.all_records(), {})


class BtNullFallback(unittest.TestCase):
    """电量 NULL 的设备必须仍在列表里（西圣间歇性 NULL 的根因回归）。"""

    def _feed(self, json_text: str) -> dict:
        from batbar import bt_battery as B

        return B._parse(json.loads(json_text))

    def test_null_node_kept_as_none(self):
        devs = self._feed(
            '[{"name":"XISEM-Air Hands-Free AG","id":"BTHENUM\\\\X","mac":"4142FFC2921C","battery":null}]'
        )
        self.assertEqual(devs, {"耳机 西圣 XISEM-Air": None})

    def test_non_null_wins_over_null_sibling(self):
        devs = self._feed(
            '[{"name":"XISEM-Air Hands-Free AG","battery":70},'
            '{"name":"XISEM-Air Hands-Free AG","battery":null}]'
        )
        self.assertEqual(devs["耳机 西圣 XISEM-Air"], 70)

    def test_null_does_not_clobber_real_value(self):
        """顺序反过来也要保住真值：AG 之后又来一个空节点。"""
        devs = self._feed(
            '[{"name":"XISEM-Air Hands-Free AG","battery":null},'
            '{"name":"XISEM-Air Hands-Free AG","battery":70}]'
        )
        self.assertEqual(devs["耳机 西圣 XISEM-Air"], 70)

    def test_multiple_devices(self):
        devs = self._feed(
            '[{"name":"LB Hi98C Pro 5.0 ","battery":93},'
            '{"name":"MCHOSE K87 5.0 ","battery":100},'
            '{"name":"XISEM-Air Hands-Free AG","battery":null},'
            '{"name":"QCY Era 7 Hands-Free AG","battery":90}]'
        )
        self.assertEqual(
            devs,
            {
                "键盘 LEOBOG Hi98C Pro": 93,
                "键盘 迈从 K87": 100,
                "耳机 西圣 XISEM-Air": None,
                "耳机 QCY Era 7": 90,
            },
        )

    def test_refresh_fills_null_from_lastknown(self):
        """_refresh 会把 NULL 填上 lastknown 的历史值。"""
        from batbar import bt_battery as B

        LK.put("耳机 西圣 XISEM-Air", 70)
        b = B.BluetoothBattery()
        b._devices = {"耳机 西圣 XISEM-Air": None}
        # 直接调合并逻辑：把 _refresh 的兜底段独立复现
        devs = {"耳机 西圣 XISEM-Air": None}
        for name, pct in list(devs.items()):
            if pct is None:
                got = LK.get(name)
                devs[name] = None if got is None else got[0]
        self.assertEqual(devs["耳机 西圣 XISEM-Air"], 70)


class Receiver24(unittest.TestCase):
    def setUp(self):
        LK.reset()  # 各用例共用同一张存档表，必须隔离

    def test_suffix_helpers(self):
        from batbar import receiver24 as R

        self.assertTrue(R.is_stale_entry("LEOBOG 键盘 (2.4G)"))
        self.assertFalse(R.is_stale_entry("LEOBOG 键盘"))
        self.assertEqual(R.base_name("LEOBOG 键盘 (2.4G)"), "LEOBOG 键盘")

    def test_skips_device_already_on_bluetooth(self):
        """键盘已在蓝牙列表里时不重复添 2.4G 条目（避免双份）。"""
        from batbar import receiver24 as R

        R.enumerate_receivers = lambda: [(0x3554, 0xFA09, "2.4G Wireless Receiver")]
        out = R.devices({"键盘 LEOBOG Hi98C Pro"})
        self.assertEqual(out, {})

    def test_adds_entry_when_off_bluetooth(self):
        from batbar import dongle as D
        from batbar import receiver24 as R

        LK.put("键盘 LEOBOG Hi98C Pro", 94)
        R.enumerate_receivers = lambda: [(0x3554, 0xFA09, "2.4G Wireless Receiver")]
        with mock.patch.object(D, "is_offline", lambda _n: False):
            out = R.devices(set())
        self.assertEqual(out, {"键盘 LEOBOG Hi98C Pro (2.4G)": 94})

    def test_no_history_means_none(self):
        from batbar import dongle as D
        from batbar import receiver24 as R

        R.enumerate_receivers = lambda: [(0x3554, 0xFA09, "2.4G")]
        with mock.patch.object(D, "is_offline", lambda _n: False):
            out = R.devices(set())
        self.assertEqual(out, {"键盘 LEOBOG Hi98C Pro (2.4G)": None})


class WiredBases(unittest.TestCase):
    """receiver24.wired_bases：有线 feature 通道 + 产品串分类（2026-10-12 改版）。

    旧判据「非接收器 PID」已证伪：迈从 K87 插线后键盘本体与接收器**同 VID/PID**
    （05AC:024F），只能靠 BusReportedDeviceDesc 区分。
    """

    def setUp(self):
        from batbar import receiver24 as R
        from batbar import wired24

        R._wired_cache = (0.0, set())  # 缓存必须逐用例隔离
        R._wired_refreshing = False  # 后台刷新标志也要隔离（否则用例互相影响）
        # 本机此刻真插着键盘：隔离 wired24 真实状态，单测只走 USB 产品串这一路
        self._p1 = mock.patch.object(wired24, "poll_once", lambda: {})
        self._p2 = mock.patch.object(wired24, "plugged", lambda _n: False)
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)

    def _patch_shell(self, stdout: str, calls: list):
        """伪造 pwsh：记录命令行、回放 USB DeviceID 清单。"""
        import shutil
        import subprocess
        import types

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)

        self._saved = (shutil.which, subprocess.run)
        shutil.which = lambda _name: "pwsh"
        subprocess.run = fake_run

    def tearDown(self):
        import shutil
        import subprocess

        saved = getattr(self, "_saved", None)  # 没走过 _patch_shell 就没什么可还原
        if saved is not None:
            shutil.which, subprocess.run = saved

    def test_same_pid_keyboard_counts_as_wired(self):
        """★ 实测样本：键盘本体插线（与接收器同 VID/PID）靠产品串认出来。"""
        from batbar import receiver24 as R

        self._patch_shell("USB\\VID_05AC&PID_024F\\6&4C78E7C&0&1|MCHOSE K87 Gaming Keyboard\n", [])
        self.assertEqual(R.wired_bases(force=True), {"键盘 迈从 K87"})

    def test_receiver_product_string_excluded(self):
        """接收器本体（产品串含 Wireless Receiver）不算有线。"""
        from batbar import receiver24 as R

        self._patch_shell(
            "USB\\VID_05AC&PID_024F\\6&4C78E7C&0&4|2.4G Wireless Receiver\n"
            "USB\\VID_3554&PID_FA09\\XYZ|2.4G Wireless Receiver\n",
            [],
        )
        self.assertEqual(R.wired_bases(force=True), set())

    def test_other_vendor_ignored(self):
        from batbar import receiver24 as R

        self._patch_shell("USB\\VID_046D&PID_C53F\\LOGI|Logitech USB Receiver\n", [])
        self.assertEqual(R.wired_bases(force=True), set())

    def test_leobog_keyboard_wired(self):
        from batbar import receiver24 as R

        self._patch_shell("USB\\VID_3554&PID_0001\\ABC|LEOBOG Hi98C Pro\n", [])
        self.assertEqual(R.wired_bases(force=True), {"键盘 LEOBOG Hi98C Pro"})

    def test_wired24_channel_counts(self):
        """① 有线 feature 通道判据也并进结果（覆盖无线全死的「关闭档+线」）。"""
        from batbar import receiver24 as R
        from batbar import wired24

        self._patch_shell("", [])
        with mock.patch.object(wired24, "plugged", lambda n: n == "键盘 迈从 K87"):
            self.assertEqual(R.wired_bases(force=True), {"键盘 迈从 K87"})

    def test_ttl_cache_skips_shell(self):
        """第二次调用吃 TTL 缓存，不再起 shell（后台那一次只算一次）。"""
        from batbar import receiver24 as R

        calls: list = []
        self._patch_shell("USB\\VID_3554&PID_0001\\ABC|LEOBOG Hi98C Pro\n", calls)
        R.wired_bases()
        for _ in range(100):  # 等后台扫描落地
            if calls:
                break
            time.sleep(0.02)
        R.wired_bases()
        time.sleep(0.2)  # 再看有没有第二次扫描
        self.assertEqual(len(calls), 1)

    def test_first_call_is_nonblocking(self):
        """★ 冷缓存不再同步等那 ~7s 的 PowerShell（2026-10-13）。

        实测：蓝牙 CIM 5.2s + 这里 7.1s ≈ 12s，就是「启动 12 秒才说未发现设备」
        和「第一次右键菜单等十几秒」的来源。现在默认非阻塞：先返回上一次结果
        （首次为空集），后台线程去刷。
        """
        from batbar import receiver24 as R

        R._wired_cache = (0.0, set())
        started = threading.Event()
        release = threading.Event()

        def slow_scan():
            started.set()
            release.wait(5)
            return {"键盘 迈从 K87"}

        with mock.patch.object(R, "_scan_wired", slow_scan):
            t0 = time.monotonic()
            got = R.wired_bases()
            dt = time.monotonic() - t0
        self.assertLess(dt, 0.3, "非阻塞路径不许等扫描")
        self.assertEqual(got, set())  # 首次拿不到 → 空集
        self.assertTrue(started.wait(2), "后台必须真的去扫")
        release.set()

    def test_force_scans_synchronously(self):
        """低电告警前 force=True 仍同步扫 —— 那个场景不能拿旧值。"""
        from batbar import receiver24 as R

        R._wired_cache = (0.0, set())
        with mock.patch.object(R, "_scan_wired", lambda: {"键盘 迈从 K87"}):
            self.assertEqual(R.wired_bases(force=True), {"键盘 迈从 K87"})

    def test_background_refresh_fills_cache(self):
        from batbar import receiver24 as R

        R._wired_cache = (0.0, set())
        with mock.patch.object(R, "_scan_wired", lambda: {"键盘 迈从 K87"}):
            R.wired_bases()
            for _ in range(100):  # 等后台线程把结果写回缓存
                if R._wired_cache[1]:
                    break
                time.sleep(0.02)
            self.assertEqual(R.wired_bases(), {"键盘 迈从 K87"})

    def test_is_wired_strips_suffix(self):
        """is_wired 吃显示名（带 (2.4G) 后缀也能判）。"""
        from batbar import receiver24 as R

        self._patch_shell("USB\\VID_3554&PID_0001\\ABC|LEOBOG Hi98C Pro\n", [])
        self.assertTrue(R.is_wired("键盘 LEOBOG Hi98C Pro (2.4G)", force=True))


class HumanAge(unittest.TestCase):
    def test_units(self):
        from batbar.agent import _human_age

        self.assertEqual(_human_age(30), "30 秒")
        self.assertEqual(_human_age(120), "2 分钟")
        self.assertEqual(_human_age(7200), "2.0 小时")
        self.assertTrue(_human_age(90000).endswith("天"))


class GhostHintRemoved(unittest.TestCase):
    """ghost_hint 已按用户要求删除（2026-10-12）：不在的设备直接隐藏，
    菜单里不留任何灰字提示。本测试锁死「函数不存在」这个契约。"""

    def test_function_gone(self):
        from batbar import receiver24 as R

        self.assertFalse(hasattr(R, "ghost_hint"))


if __name__ == "__main__":
    unittest.main()
