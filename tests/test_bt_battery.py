"""bt_battery：JSON 解析与友好名映射（不依赖真实 PowerShell——注入假输出）。"""
from __future__ import annotations

import unittest
from unittest import mock

from batbar import bt_battery as B


class Friendly(unittest.TestCase):
    def test_nickname_table(self):
        # 命名规范（2026-10-12）：「类别 品牌+型号」
        self.assertEqual(B.friendly("LB Hi98C Pro 5.0 "), "键盘 LEOBOG Hi98C Pro")
        self.assertEqual(B.friendly("QCY Era 7 Hands-Free AG"), "耳机 QCY Era 7")
        self.assertEqual(B.friendly("WH-1000XM5"), "耳机 Sony")
        self.assertEqual(B.friendly("XISEM-Air Hands-Free AG"), "耳机 西圣 XISEM-Air")
        self.assertEqual(B.friendly("MCHOSE K87 5.0"), "键盘 迈从 K87")

    def test_noise_strip(self):
        # 未收录设备：剥协议后缀而不是原样吐出长名
        self.assertEqual(B.friendly("SomeBrand Hands-Free AG"), "SomeBrand")

    def test_unknown_passthrough(self):
        self.assertEqual(B.friendly("FooBar"), "FooBar")


class Parse(unittest.TestCase):
    def _feed(self, json_text: str) -> B.BluetoothBattery:
        """绕过 subprocess：直接喂 ConvertTo-Json 的假输出。"""
        import json
        b = B.BluetoothBattery()
        data = json.loads(json_text)
        devs = {}
        for d in data or []:
            if isinstance(d, dict) and d.get("battery") is not None:
                devs[B.friendly(str(d.get("name") or "BT"))] = max(0, min(100, int(d["battery"])))
        b._devices = devs
        import time
        b._ts = time.monotonic()
        return b

    def test_array(self):
        b = self._feed('[{"name":"LB Hi98C Pro 5.0","id":"BTHLE\\\\DEV_X","battery":93},'
                       '{"name":"QCY Era 7 Hands-Free AG","id":"BTHENUM\\\\Y","battery":90}]')
        self.assertEqual(b.all_devices(), {"键盘 LEOBOG Hi98C Pro": 93,
                                           "耳机 QCY Era 7": 90})

    def test_clamp(self):
        b = self._feed('[{"name":"X","battery":120}]')
        self.assertEqual(b.all_devices()["X"], 100)

    def test_empty(self):
        b = self._feed('[]')
        self.assertEqual(b.all_devices(), {})


class ConnFilter(unittest.TestCase):
    """连接态过滤：未连接的蓝牙设备不显示（2026-10-12 用户需求）。

    样本 MAC/连接态取自 2026-10-12 18:2x 本机实测（与 Windows 设置页逐台一致）：
      QCY Era 7    84AC6098BBDC  Connected
      XISEM-Air    4142FFC2921C  Disconnected（在充电盒里）
      LB Hi98C Pro E33312B589DA  Disconnected（切到 2.4G）
      MCHOSE K87   DBA28EF0FD50  Disconnected（切到 2.4G）
    """

    def test_disconnected_dropped(self):
        rows = [
            {"name": "XISEM-Air Hands-Free AG", "mac": "4142FFC2921C", "battery": 10},
            {"name": "QCY Era 7 Hands-Free AG", "mac": "84AC6098BBDC", "battery": 100},
        ]
        conn = {"4142FFC2921C": False, "84AC6098BBDC": True}
        self.assertEqual(B._parse(rows, connected=conn), {"耳机 QCY Era 7": 100})

    def test_disconnected_keyboard_dropped(self):
        rows = [{"name": "MCHOSE K87 5.0", "mac": "DBA28EF0FD50", "battery": 100}]
        self.assertEqual(B._parse(rows, connected={"DBA28EF0FD50": False}), {})

    def test_unknown_mac_kept(self):
        """MAC 不在表里（连接态查询失败/新设备）= 未知 → 保留（fail-open）。"""
        rows = [{"name": "QCY Era 7 Hands-Free AG", "mac": "84AC6098BBDC", "battery": 100}]
        self.assertEqual(B._parse(rows, connected={}), {"耳机 QCY Era 7": 100})

    def test_no_conn_table_keeps_everything(self):
        """不传连接态表（旧调用路径/单测）= 不过滤。"""
        rows = [{"name": "XISEM-Air Hands-Free AG", "mac": "4142FFC2921C", "battery": 10}]
        self.assertEqual(B._parse(rows), {"耳机 西圣 XISEM-Air": 10})


class ConnJudge(unittest.TestCase):
    """连接态判据 = classic ∪ le（两个 WinRT API 在同一个 PS 进程内合并）。

    bug 14（2026-10-13 13:14 用户报「蓝牙关机后点立即刷新要等三四秒才消失」）：
    旧判据②「活跃 HID 派生节点」要**另起一个 ~2s 的 CIM 进程**，且蓝牙 HID
    设备**关机后节点 Present 不及时移除** → 把已关机误判成在连，快判
    （drop_disconnected_now）失效、退化成等 CIM 全量查询（~3-4s）。
    全景枚举实测（`probes/probe_bt_all_status.py`）：le API 对在连 BLE 报
    Connected、对离线 BLE 报 Disconnected，准确且不陈旧 → 取代 hid 补证。
    现在 Python 侧只解析 PS 合并后的 {mac: bool}，不再做任何补证。
    """

    def setUp(self):
        B._conn_cache = (0.0, {})

    def _fake_shell(self, json_text: str):
        import shutil
        import subprocess
        import types
        self._saved = (shutil.which, subprocess.run)
        shutil.which = lambda _n: "powershell"
        subprocess.run = lambda *a, **k: types.SimpleNamespace(
            stdout=json_text, stderr="", returncode=0)
        self.addCleanup(self._restore)

    def _restore(self):
        import shutil
        import subprocess
        shutil.which, subprocess.run = self._saved

    def test_parses_merged_classic_le_result(self):
        """Python 侧忠实解析 PS 合并结果（classic∪le），不再做任何补证。

        PS 脚本已把「BLE 键盘 classic 假阴性、le 兜住」合并成一个 bool，
        Python 侧只透传——LEOBOG（BLE 在连）=True、XISEM（经典离线）=False。
        """
        self._fake_shell('{"E33312B589DA": true, "4142FFC2921C": false}')
        got = B.connected_macs(["E33312B589DA", "4142FFC2921C"], force=True)
        self.assertTrue(got["E33312B589DA"])     # BLE 键盘：le 报在连
        self.assertFalse(got["4142FFC2921C"])    # 经典耳机：确实没连

    def test_winrt_true_still_works(self):
        """经典蓝牙耳机在连 → True。"""
        self._fake_shell('{"84AC6098BBDC": true}')
        got = B.connected_macs(["84AC6098BBDC"], force=True)
        self.assertTrue(got["84AC6098BBDC"])

    def test_offline_ble_reports_false_not_stale(self):
        """离线 BLE 设备 → False（bug 14 核心：不再被 hid 陈旧节点翻成在连）。

        对照全景枚举 DBA28E：离线 BLE，classic/le 都报 Disconnected →
        合并 False。旧 hid 补证会因节点 Present 残留误报 True，现已移除。
        """
        self._fake_shell('{"DBA28EF0FD50": false}')
        got = B.connected_macs(["DBA28EF0FD50"], force=True)
        self.assertFalse(got["DBA28EF0FD50"])

    def test_ps_conn_script_wires_le_api(self):
        """契约：连接态 PS 脚本必须含 BLE 专用 API（否则 BLE 假阴性无人兜）。"""
        self.assertIn("BluetoothLEDevice", B._PS_CONN)
        self.assertIn("BluetoothDevice", B._PS_CONN)   # 经典判据仍在
        self.assertNotIn("_PS_HIDMACS", dir(B))        # hid 补证已删干净


class ForceRefresh(unittest.TestCase):
    """「立即刷新」强刷（2026-10-12）：耳机刚连上时 WinRT 连接态可能还是
    False，15s 连接态缓存 + 20s TTL 会让它进不了列表；用户点了刷新就该
    立刻看到 —— force_refresh 必须绕过两层缓存。"""

    def test_force_refresh_bypasses_ttl(self):
        import time
        from unittest import mock
        b = B.BluetoothBattery()
        b._devices = {"旧": 1}
        b._ts = time.monotonic()             # TTL 内：prewarm 会直接返回
        calls = []
        with mock.patch.object(b, "_run_query",
                               lambda force_conn=False, quick=False:
                               calls.append((force_conn, quick))):
            b.prewarm()                      # TTL 内 → 不触发查询
            b.force_refresh()                # 强刷 → 必查，且 force_conn=True
            b.force_refresh(quick=True)      # bug 13：单枪快查透传
        self.assertEqual(calls, [(True, False), (True, True)])

    def test_run_query_forces_conn_query(self):
        import json
        import types
        from unittest import mock
        b = B.BluetoothBattery()
        b._shell = "powershell"
        rows = [{"name": "QCY Era 7 Hands-Free AG", "mac": "84AC6098BBDC",
                 "battery": 90}]
        seen = []

        def fake_conn(macs, force=False):
            seen.append((list(macs), force))
            return {"84AC6098BBDC": True}

        fake_ps = lambda *a, **k: types.SimpleNamespace(
            stdout=json.dumps(rows), stderr="", returncode=0)
        with mock.patch.object(B.subprocess, "run", fake_ps), \
             mock.patch.object(B, "connected_macs", fake_conn):
            b._run_query(force_conn=True)
        self.assertEqual(seen, [(["84AC6098BBDC"], True)])
        self.assertEqual(b._devices, {"耳机 QCY Era 7": 90})


class EmptyRetention(unittest.TestCase):
    """空查询结果在**宽限期内**不清空旧列表（2026-10-13 修「列表忽有忽无」）。

    WMI 查询间歇性返回空表（实测：连着的 LEOBOG/QCY 上一轮还在、下一轮
    整表空），照单全收 → 弹菜单读到空缓存 = 「未发现设备」，而托盘图标还挂着
    旧数字 = 「图标有、列表没有」。

    ⚠️ 判据必须是**时间**：空结果返回极快，旧的「连续 3 次」判据 7 秒就满足
    （2026-10-13 01:18 用户实测日志：01:18:27 / :31 / :34 三次空 → 清空）。
    """

    def _mk(self, devs: dict) -> B.BluetoothBattery:
        import time
        b = B.BluetoothBattery()
        b._devices = dict(devs)
        b._ts = time.monotonic()
        b._last_ok_ts = time.monotonic()      # 刚成功过 → 处于宽限期内
        return b

    def test_empty_keeps_last_list(self):
        b = self._mk({"耳机 QCY Era 7": 90, "键盘 LEOBOG Hi98C Pro": 99})
        b._accept({})
        self.assertEqual(b._devices, {"耳机 QCY Era 7": 90,
                                      "键盘 LEOBOG Hi98C Pro": 99})

    def test_many_empties_within_grace_keep_list(self):
        """宽限期内**多少次**空结果都保留（这正是旧实现翻车的地方）。"""
        b = self._mk({"键盘 LEOBOG Hi98C Pro": 99})
        for _ in range(30):
            b._accept({})
        self.assertEqual(b._devices, {"键盘 LEOBOG Hi98C Pro": 99})

    def test_stale_list_accepted_as_offline(self):
        """空结果持续超过宽限期 → 接受真·离线（否则断开也一直挂着旧值）。"""
        import time
        b = self._mk({"耳机 QCY Era 7": 90})
        b._last_ok_ts = time.monotonic() - B.EMPTY_GRACE - 1
        b._accept({})
        self.assertEqual(b._devices, {})

    def test_nonempty_resets_streak(self):
        b = self._mk({"耳机 QCY Era 7": 90})
        b._accept({})
        b._accept({"耳机 QCY Era 7": 89})
        self.assertEqual(b._empty_streak, 0)
        self.assertEqual(b._devices, {"耳机 QCY Era 7": 89})

    def test_fresh_empty_stays_empty(self):
        """初始（从未查到过设备）空结果直接落盘，不算抖动。"""
        b = self._mk({})
        b._accept({})
        self.assertEqual(b._devices, {})
        self.assertEqual(b._empty_streak, 0)

    def test_run_query_empty_stdout_keeps_cache(self):
        """端到端：_run_query 拿到空 stdout（PS 抽风）也不清旧缓存。"""
        b = self._mk({"耳机 QCY Era 7": 90})
        b._shell = "pwsh-fake"

        def fake_run(*_a, **_k):
            import types
            return types.SimpleNamespace(stdout="", stderr="")

        from unittest import mock
        with mock.patch.object(B.subprocess, "run", fake_run), \
             mock.patch.object(B, "connected_macs", lambda *a, **k: {}):
            b._run_query()
        self.assertEqual(b._devices, {"耳机 QCY Era 7": 90})


class EmptyVerifyByWinRT(unittest.TestCase):
    """★ bug 12（2026-10-13 11:41 用户报）：关掉**最后一台**蓝牙设备后，
    「立即刷新」和等待都杀不掉条目。

    机制：还剩 ≥1 台蓝牙时关掉一台，CIM 查询仍返回非空表 → 不进宽限，
    4-5s 消失（用户实测正常）；关掉最后一台，其 PnP 节点整表消失 →
    CIM 返回**空表** → 掉进 EMPTY_GRACE=180s 防抖宽限 → 卡满 3 分钟。
    空表自己分不清「抖动」与「真离线」，但 WinRT 连接态分得清
    （现场取证 `probes/probe_bt_last_device.py`：键盘关机后整表空、
    WinRT 如实报 false）→ 宽限期内复核全离线就立即接受空表。
    """

    def _mk(self, devs: dict) -> B.BluetoothBattery:
        import time
        b = B.BluetoothBattery()
        b._devices = dict(devs)
        b._ts = time.monotonic()
        b._last_ok_ts = time.monotonic()      # 刚成功过 → 宽限期内
        b._mac_by_name = {"键盘 LEOBOG Hi98C Pro": "E33312B589DA"}
        return b

    def test_all_offline_cleared_immediately(self):
        """宽限期内空表 + WinRT 复核全离线 → 立即清空（不等 180s）。"""
        import time
        from unittest import mock
        b = self._mk({"键盘 LEOBOG Hi98C Pro": 98})
        seen = []
        fake = lambda macs, force=False: seen.append(
            (list(macs), force)) or {"E33312B589DA": False}
        with mock.patch.object(B, "connected_macs", fake):
            b._accept({})
        self.assertEqual(b._devices, {})
        # 复核必须 force=True：宽限里可能撑 180s，不能吃 15s 旧缓存
        self.assertEqual(seen, [(["E33312B589DA"], True)])

    def test_still_connected_keeps_list(self):
        """WinRT 说设备还在连 → 空表是 CIM 抖动 → 维持宽限保留列表。"""
        from unittest import mock
        b = self._mk({"键盘 LEOBOG Hi98C Pro": 98})
        with mock.patch.object(B, "connected_macs",
                               lambda *a, **k: {"E33312B589DA": True}):
            b._accept({})
        self.assertEqual(b._devices, {"键盘 LEOBOG Hi98C Pro": 98})

    def test_conn_query_failed_keeps_list(self):
        """连接态问不出（失败/超时）= 未知 → fail-safe 维持宽限。"""
        from unittest import mock
        b = self._mk({"键盘 LEOBOG Hi98C Pro": 98})
        with mock.patch.object(B, "connected_macs", lambda *a, **k: {}):
            b._accept({})
        self.assertEqual(b._devices, {"键盘 LEOBOG Hi98C Pro": 98})

    def test_no_mac_mapping_keeps_list(self):
        """没有 MAC 可问（映射还没攒出来）→ 无法复核 → 维持宽限。"""
        b = self._mk({"神秘设备": 50})
        b._mac_by_name = {}
        b._accept({})
        self.assertEqual(b._devices, {"神秘设备": 50})

    def test_run_query_records_mac_map(self):
        """_run_query 成功查询时记下「友好名 → MAC」，空表复核靠它反查。"""
        import json
        import time
        import types
        from unittest import mock
        b = B.BluetoothBattery()
        b._shell = "pwsh-fake"
        rows = [{"name": "LB Hi98C Pro 5.0 ", "mac": "E33312B589DA",
                 "battery": 98}]

        def fake_run(*_a, **_k):
            return types.SimpleNamespace(stdout=json.dumps(rows), stderr="")

        with mock.patch.object(B.subprocess, "run", fake_run), \
             mock.patch.object(B, "connected_macs",
                               lambda *a, **k: {"E33312B589DA": True}):
            b._run_query()
        self.assertEqual(b._mac_by_name,
                         {"键盘 LEOBOG Hi98C Pro": "E33312B589DA"})
        self.assertEqual(b._devices, {"键盘 LEOBOG Hi98C Pro": 98})

        # 端到端：下一轮整表空（最后一台关机）+ WinRT 报离线 → 当场清空
        b._last_ok_ts = time.monotonic()      # 确保还在宽限期内

        def fake_empty(*_a, **_k):
            return types.SimpleNamespace(stdout="", stderr="")

        with mock.patch.object(B.subprocess, "run", fake_empty), \
             mock.patch.object(B, "connected_macs",
                               lambda *a, **k: {"E33312B589DA": False}):
            b._run_query()
        self.assertEqual(b._devices, {})


class QuickQuery(unittest.TestCase):
    """bug 13：「立即刷新」走 CIM **单枪**快查（跳过 200ms 重试），
    电量值刷新 ~2s 完成；后台预热照旧三枪保精度。"""

    def _mk(self):
        b = B.BluetoothBattery()
        b._shell = "pwsh-fake"
        b._ts = 0.0
        return b

    def _run_capture(self, b, **kw):
        import types
        from unittest import mock
        seen = []

        def fake_run(cmd, **_k):
            seen.append(cmd[-1])          # PS 脚本全文
            return types.SimpleNamespace(stdout="null", stderr="")

        with mock.patch.object(B.subprocess, "run", fake_run):
            b._run_query(**kw)
        return seen

    def test_force_refresh_injects_single_shot(self):
        b = self._mk()
        scripts = self._run_capture(b, force_conn=True, quick=True)
        self.assertTrue(any("$tries = 1" in s for s in scripts))
        self.assertFalse(any("$tries = 3" in s for s in scripts))

    def test_background_keeps_three_shots(self):
        b = self._mk()
        scripts = self._run_capture(b)    # 后台预热 = 默认三枪
        self.assertTrue(any("$tries = 3" in s for s in scripts))


class DropDisconnectedNow(unittest.TestCase):
    """bug 13：「立即刷新」的前置快判——WinRT 连接态确认离线的条目
    **立即摘除**（~1.5s），不等 CIM 电量查询。真在连的绝不误摘，
    连接态问不出（失败）就什么都不动（fail-safe）。"""

    def _mk(self):
        import time
        b = B.BluetoothBattery()
        b._devices = {"耳机 QCY Era 7": 90, "键盘 LEOBOG Hi98C Pro": 98}
        b._mac_by_name = {"耳机 QCY Era 7": "84AC6098BBDC",
                          "键盘 LEOBOG Hi98C Pro": "E33312B589DA"}
        b._ts = time.monotonic()
        return b

    def test_confirmed_offline_dropped(self):
        from unittest import mock
        b = self._mk()
        seen = []

        def fake(macs, force=False):
            seen.append((list(macs), force))
            return {m: False for m in macs}

        with mock.patch.object(B, "connected_macs", fake):
            dropped = b.drop_disconnected_now()
        self.assertEqual(sorted(dropped), ["耳机 QCY Era 7", "键盘 LEOBOG Hi98C Pro"])
        self.assertEqual(b._devices, {})
        self.assertEqual(seen, [(["84AC6098BBDC", "E33312B589DA"], True)])

    def test_partial_offline_drops_only_those(self):
        from unittest import mock
        b = self._mk()
        with mock.patch.object(
                B, "connected_macs",
                lambda macs, force=False: {"84AC6098BBDC": False,
                                           "E33312B589DA": True}):
            dropped = b.drop_disconnected_now()
        self.assertEqual(dropped, ["耳机 QCY Era 7"])
        self.assertEqual(b._devices, {"键盘 LEOBOG Hi98C Pro": 98})

    def test_query_failed_noop(self):
        from unittest import mock
        b = self._mk()
        with mock.patch.object(B, "connected_macs", lambda *a, **k: {}):
            self.assertEqual(b.drop_disconnected_now(), [])
        self.assertEqual(len(b._devices), 2)      # 原样保留

    def test_no_devices_or_no_mac_map_noop(self):
        b = self._mk()
        b._devices = {}
        self.assertEqual(b.drop_disconnected_now(), [])
        b._devices = {"神秘设备": 1}
        b._mac_by_name = {}                       # 没有 MAC 可问
        self.assertEqual(b.drop_disconnected_now(), [])
        self.assertEqual(b._devices, {"神秘设备": 1})


class SyncNow(unittest.TestCase):
    """bug 15：「立即刷新」双向快判——**一次** WinRT 查询同时摘离线 + 加上线。

    对称于 bug 14（下线秒消失）：上线也要秒出现，且不能因拆成 add+drop
    两次查询把下线时延翻倍。候选来自 `_mac_by_name`（只增不减），
    连接态问不出（{}）全不动（fail-open）。"""

    def _mk(self):
        import time
        b = B.BluetoothBattery()
        # 耳机在列表里；键盘记得 MAC 但当前不在列表（之前断开被摘了）
        b._devices = {"耳机 QCY Era 7": 90}
        b._mac_by_name = {"耳机 QCY Era 7": "84AC6098BBDC",
                          "键盘 LEOBOG Hi98C Pro": "E33312B589DA"}
        b._ts = time.monotonic()
        return b

    def test_adds_online_and_drops_offline_in_one_query(self):
        """耳机关机 + 键盘开机，同一次查询里一摘一加。"""
        from unittest import mock
        b = self._mk()
        seen = []

        def fake(macs, force=False):
            seen.append((sorted(macs), force))
            return {"84AC6098BBDC": False,       # 耳机离线
                    "E33312B589DA": True}         # 键盘上线

        with mock.patch.object(B, "connected_macs", fake), \
             mock.patch.object(B.lastknown, "get",
                               lambda n: (98, 0.0) if "LEOBOG" in n else None):
            added, dropped = b.sync_now()
        self.assertEqual(dropped, ["耳机 QCY Era 7"])
        self.assertEqual(added, ["键盘 LEOBOG Hi98C Pro"])
        self.assertEqual(b._devices, {"键盘 LEOBOG Hi98C Pro": 98})  # lastknown 兜底
        self.assertIn("键盘 LEOBOG Hi98C Pro", b._fast_add_protect)   # 闪回保护已挂
        # 只 spawn 一次查询，两个方向的 MAC 都在里面
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], ["84AC6098BBDC", "E33312B589DA"])
        self.assertTrue(seen[0][1])               # force=True，不吃 15s 缓存

    def test_add_without_lastknown_gives_none(self):
        """上线设备没有历史电量 → 先给 None（真值随后 CIM 刷新）。"""
        from unittest import mock
        b = self._mk()
        b._devices = {}                           # 列表空，只有键盘待上线
        with mock.patch.object(B, "connected_macs",
                               lambda macs, force=False: {"E33312B589DA": True}), \
             mock.patch.object(B.lastknown, "get", lambda n: None):
            added, dropped = b.sync_now()
        self.assertEqual(added, ["键盘 LEOBOG Hi98C Pro"])
        self.assertEqual(dropped, [])
        self.assertIsNone(b._devices["键盘 LEOBOG Hi98C Pro"])

    def test_already_connected_stays_no_double_add(self):
        """在列表 + 报在连 → 不动（不重复加、不摘）。"""
        from unittest import mock
        b = self._mk()
        with mock.patch.object(B, "connected_macs",
                               lambda macs, force=False: {
                                   "84AC6098BBDC": True,   # 耳机在连
                                   "E33312B589DA": False}):  # 键盘仍离线
            added, dropped = b.sync_now()
        self.assertEqual(added, [])
        self.assertEqual(dropped, [])
        self.assertEqual(b._devices, {"耳机 QCY Era 7": 90})   # 原样

    def test_query_failed_noop(self):
        """连接态问不出（{}）→ 全不动（fail-open，不误摘不误加）。"""
        from unittest import mock
        b = self._mk()
        with mock.patch.object(B, "connected_macs", lambda *a, **k: {}):
            self.assertEqual(b.sync_now(), ([], []))
        self.assertEqual(b._devices, {"耳机 QCY Era 7": 90})

    def test_partial_conn_unknown_mac_skipped(self):
        """某 MAC 没问到（不在返回里）→ 那台不动，其余照常和解。"""
        from unittest import mock
        b = self._mk()
        # 只返回耳机的连接态，键盘的 MAC 缺失
        with mock.patch.object(B, "connected_macs",
                               lambda macs, force=False: {"84AC6098BBDC": False}):
            added, dropped = b.sync_now()
        self.assertEqual(dropped, ["耳机 QCY Era 7"])
        self.assertEqual(added, [])               # 键盘未知 → 不加

    def test_no_mac_map_noop(self):
        b = self._mk()
        b._mac_by_name = {}
        self.assertEqual(b.sync_now(), ([], []))

    def test_device_without_mac_entry_preserved(self):
        """列表里有条目但 `_mac_by_name` 没有它的 MAC → 无法判定，保留不碰。"""
        from unittest import mock
        b = self._mk()
        b._devices = {"耳机 QCY Era 7": 90, "神秘设备": 50}
        b._mac_by_name = {"耳机 QCY Era 7": "84AC6098BBDC"}   # 神秘设备无 MAC
        with mock.patch.object(B, "connected_macs",
                               lambda macs, force=False: {"84AC6098BBDC": True}):
            added, dropped = b.sync_now()
        self.assertEqual((added, dropped), ([], []))
        self.assertEqual(b._devices, {"耳机 QCY Era 7": 90, "神秘设备": 50})


class PreserveFastAdds(unittest.TestCase):
    """bug 15 闪回保护：CIM 整体替换 `_devices` 时，保住快判刚加的、这一轮
    CIM 还没扫到的设备（PnP 节点建立滞后于 WinRT 连接态）——否则用户看到
    「闪现又消失」。CIM 扫到真实行 → 保护失效以 CIM 为准；保护期过 → 移除。"""

    def _mk(self, protect_until_offset=100.0):
        import time
        b = B.BluetoothBattery()
        b._devices = {"键盘 LEOBOG Hi98C Pro": 98}   # 快判刚加的（兜底值 98）
        b._mac_by_name = {"键盘 LEOBOG Hi98C Pro": "E33312B589DA"}
        b._fast_add_protect = {
            "键盘 LEOBOG Hi98C Pro": time.monotonic() + protect_until_offset}
        b._ts = time.monotonic()
        return b

    def test_preserved_when_cim_misses_within_window(self):
        """保护期内 + CIM 本轮没扫到 → 沿用兜底值，不删。"""
        b = self._mk(protect_until_offset=100.0)
        out = b._preserve_fast_adds({"耳机 QCY Era 7": 100})   # CIM 只有耳机
        self.assertEqual(out["键盘 LEOBOG Hi98C Pro"], 98)     # 保住了
        self.assertEqual(out["耳机 QCY Era 7"], 100)
        self.assertIn("键盘 LEOBOG Hi98C Pro", b._fast_add_protect)  # 标记仍在

    def test_protection_cleared_when_cim_has_it(self):
        """CIM 本轮扫到了 → 保护失效，以 CIM 真实读数为准。"""
        b = self._mk(protect_until_offset=100.0)
        out = b._preserve_fast_adds({"键盘 LEOBOG Hi98C Pro": 95})  # CIM 真值 95
        self.assertEqual(out["键盘 LEOBOG Hi98C Pro"], 95)     # CIM 覆盖兜底值
        self.assertNotIn("键盘 LEOBOG Hi98C Pro", b._fast_add_protect)  # 标记清了

    def test_removed_when_protection_expired(self):
        """保护期已过 + CIM 仍没扫到 → 移除（交常规逻辑判定）。"""
        b = self._mk(protect_until_offset=-1.0)   # 已过期
        out = b._preserve_fast_adds({"耳机 QCY Era 7": 100})
        self.assertNotIn("键盘 LEOBOG Hi98C Pro", out)          # 被移除
        self.assertNotIn("键盘 LEOBOG Hi98C Pro", b._fast_add_protect)

    def test_noop_without_protect_marks(self):
        """没有任何保护标记 → 原样返回（热路径零开销）。"""
        b = self._mk()
        b._fast_add_protect = {}
        devs = {"耳机 QCY Era 7": 100}
        self.assertIs(b._preserve_fast_adds(devs), devs)       # 同一对象，未拷贝


class CachedDevicesFirstCall(unittest.TestCase):
    """`cached_devices()` 是"不阻塞版"，**任何**调用都不能把菜单线程拖住。

    2026-10-16 修的 bug：首次调用（冷缓存）会同步跑一次 ~4s 的 CIM 查询，
    或在 `_refreshing` 时空转最多 5s —— 而调用方
    `agent.devices_snapshot()` 是被 pystray 菜单工厂**同步**调的，等于启动后
    第一次点开托盘菜单卡 4~5 秒；卡完仍无结果就返回空表（蓝牙条目整批消失）。
    """
    WAIT = 0.15          # 把 _WAIT_FIRST 压到 0.15s，测试不用真等 2s

    def _fresh(self) -> B.BluetoothBattery:
        b = B.BluetoothBattery()
        b._ts = 0.0  # 冷缓存 = 从未取过
        return b

    def test_does_not_run_query_inline(self):
        """冷缓存 + 没人预热 → 只触发后台预热，绝不在调用方线程跑查询。"""
        b = self._fresh()
        calls = []
        with mock.patch.object(b, "prewarm", lambda: calls.append("prewarm")), \
             mock.patch.object(b, "_refresh",
                                    lambda: self.fail("首次调用不该同步跑 _refresh")), \
             mock.patch.object(B, "_WAIT_FIRST", self.WAIT):
            self.assertEqual({}, b.cached_devices())
        self.assertEqual(["prewarm"], calls)

    def test_waits_for_inflight_prewarm_but_bounded(self):
        """预热线程已在跑 → 等它，但**必须**在 _WAIT_FIRST 内返回。"""
        import time
        b = self._fresh()
        b._refreshing = True  # 假装有人在查（且一直查不出来）
        t0 = time.monotonic()
        with mock.patch.object(B, "_WAIT_FIRST", self.WAIT):
            self.assertEqual({}, b.cached_devices())
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, self.WAIT + 0.5,
                        f"等了 {elapsed:.2f}s，超过上限 —— 菜单又会被冻住")

    def test_falls_back_to_lastknown_instead_of_empty(self):
        """超时仍无结果 → 用 last-known 顶上，别让蓝牙条目整批消失。"""
        from batbar import lastknown
        lastknown.put("耳机 QCY Era 7", 82)
        lastknown.put("键盘 LEOBOG Hi98C Pro", 55)
        b = self._fresh()
        b._refreshing = True
        with mock.patch.object(B, "_WAIT_FIRST", self.WAIT):
            got = b.cached_devices()
        self.assertEqual({"耳机 QCY Era 7": 82, "键盘 LEOBOG Hi98C Pro": 55}, got)

    def test_no_fallback_when_real_data_exists(self):
        """已经有真数据 → 直接吃缓存，不掺 last-known（旧值不能盖真值）。"""
        from batbar import lastknown
        lastknown.put("耳机 QCY Era 7", 82)
        b = self._fresh()
        b._devices = {"耳机 QCY Era 7": 10}
        b._ts = 1.0
        with mock.patch.object(b, "prewarm", lambda: None):
            self.assertEqual({"耳机 QCY Era 7": 10}, b.cached_devices())


if __name__ == "__main__":
    unittest.main()
