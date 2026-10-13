"""2.4G 私有接收器的设备识别（三模键盘切到 dongle 后的电量兜底）。

── 为什么需要它 ─
三模键盘（蓝牙 / 2.4G / 有线）只有**蓝牙**那条链路会向 Windows 上报电量：

  * BLE 模式：HID over GATT 的 Battery Service，Windows 写进
    BTHLE 节点的 DEVPKEY_Device_BatteryLevel；
  * 经典蓝牙：HFP AG 的电量上报，同样落到 BTHENUM 节点。
  * **2.4G 模式：什么都不报** —— 实测（2026-10-12）本机三张 relevant 设备
    （3554:FA09 接收器、3554:FA07 与 05AC:024F 的 BLE 节点）**全部没有**
    DEVPKEY_Device_BatteryLevel，也没有标准 HID Battery Strength usage
    (0x0C:0x0091)。Windows 自身在 2.4G 下对键盘电量一无所知。

── 实时电量其实拿得到（2026-10-12 修正） ──
上面那段「2.4G 什么都不报」只对 Windows 内部审计成立 —— Windows 确实不知道，
但**接收器自己知道**：它会把键盘的事件帧经 `0xFF02` 厂商通道推上来，其中
`type=0x05` 就是电量（详见 `dongle.py`）。之前判「无实时通道」是错的：当时
`CreateFileW` 只给了 GENERIC_READ，写不出探测帧，于是误判。

── 本模块现在做什么 ──
  1. 识别接收器是否插着（枚举 HID 的 VID/PID + 产品串）；
  2. 读数取「最可信的那个」：`dongle` 的实时帧 > `lastknown` 的历史存档；
  3. **离线判定**：这条通道是事件驱动的，键盘真关机后完全零流量，所以
     `dongle.is_offline()` 为真时**不再显示该条目**（旧实现只看「接收器插着」，
     那是个恒真条件，于是关机了菜单里还挂着读数 —— 用户报的 bug）。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import POINTER, byref, wintypes

from .dongle import TARGETS as KNOWN_RECEIVERS
from . import win32ctypes as w32

log = logging.getLogger("batbar.r24")

SUFFIX = " (2.4G)"          # 菜单里区分连接方式的后缀
WIRED_SUFFIX = " (有线)"     # 插线充电形态（device-battery-tray 借鉴，2026-10-12）

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

# 一次全量 USB 枚举要起 PowerShell（~0.5-1s），菜单每 2s 可能重算 —— 必须缓存。
# ⚠️ 本机实测（2026-10-13）：这条 PowerShell 查询冷启动要 **~7s**（不是 0.5-1s），
# 而它过去是**同步**跑在 `devices_snapshot()` 里的 ⇒ 首轮快照要 5.2s(蓝牙 CIM)
# + 7s(这里) ≈ **12s**，直接表现成「启动后 12 秒才说『未发现设备』」和「第一次
# 右键菜单要等十几秒」。所以现在默认**非阻塞**：先返回上一次结果，后台线程去刷。
_wired_cache: tuple[float, set[str]] = (0.0, set())
_WIRED_TTL = 15.0
_wired_refreshing = False


def _target_vendor_ids() -> set[int]:
    from .dongle import TARGETS
    return {vid for (vid, _pid) in TARGETS}


def _scan_wired() -> set[str]:
    """真去查一次「哪些键盘此刻插着 USB 线」（两路并集，见 `wired_bases` 文档）。

    ⚠️ 冷启动实测 ~7s（PowerShell 全量 USB 枚举），**不许在快照/菜单路径里同步调**。
    """
    from .dongle import TARGETS, WIRED_TARGETS
    from . import wired24
    result: set[str] = set()

    # ① 有线 feature 通道（force 时当场重扫一次：低电告警前必须拿实时态）
    try:
        wired24.poll_once()
        result |= {n for n in set(TARGETS.values()) | set(WIRED_TARGETS.values())
                   if wired24.plugged(n)}
    except Exception as exc:  # noqa: BLE001 - 有线通道失败不影响 USB 判据
        log.debug("wired24 detect: %s", exc)

    # ② USB 节点 + 产品串分类
    vendor_ids = {vid for (vid, _pid) in TARGETS} | \
        {vid for (vid, _pid) in WIRED_TARGETS}
    try:
        import re
        import shutil
        import subprocess
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell:
            q = (
                "$ErrorActionPreference='SilentlyContinue';"
                "Get-CimInstance Win32_PnPEntity -Filter \"DeviceID LIKE 'USB%'\" "
                "| Where-Object { $_.Present } | ForEach-Object {"
                "  $b = Get-PnpDeviceProperty -InstanceId $_.InstanceId "
                "-KeyName 'DEVPKEY_Device_BusReportedDeviceDesc' "
                "-ErrorAction SilentlyContinue;"
                "  \"$($_.DeviceID)|$($b.Data)\" }"
            )
            r = subprocess.run([shell, "-NoProfile", "-Command", q],
                               capture_output=True, text=True, timeout=15,
                               encoding="utf-8", errors="replace")
            for line in (r.stdout or "").splitlines():
                dev_id, _, bus = line.strip().partition("|")
                m = re.search(r"VID_([0-9A-F]{4})", dev_id.upper())
                if not m:
                    continue
                vid = int(m.group(1), 16)
                if vid not in vendor_ids:
                    continue
                if any(h in bus.lower() for h in ("receiver", "dongle", "接收器")):
                    continue                     # 这是接收器本体，不是键盘插线
                for table in (TARGETS, WIRED_TARGETS):
                    for (v, _p), name in table.items():
                        if v == vid:
                            result.add(name)
    except Exception as exc:  # noqa: BLE001 - 有线判定失败不影响主链路
        log.debug("wired_bases: %s", exc)
    return result


def wired_bases(*, force: bool = False) -> set[str]:
    """当前**插着 USB 线**的目标键盘友好名集合（TTL 缓存）。

    ── 判据演进（2026-10-12）──
    初版判据「厂商 VID 出现在**非接收器 PID** 的 USB 节点上」**是错的**：
    实测迈从 K87 插线后，键盘本体枚举成 USB 设备时**与接收器共用 VID/PID**
    （`05AC:024F`），只有产品串能区分（`2.4G Wireless Receiver` vs
    `MCHOSE K87 Gaming Keyboard`）——于是插着线也判不出「有线」。
    现在两路并集：
      ① `wired24.plugged()`：键盘暴露的 520B 有线 feature 接口（权威，
         还能顺手读到有线电量与充电标志，覆盖「关闭档 + 线」无线全死的情况）；
      ② 产品串分类：同厂商 VID 的 USB 节点里，BusReportedDeviceDesc **不像
         接收器**（不含 receiver/dongle/接收器）的那些 —— 兜住没有有线
         feature 通道的型号。
    插线即有线供电，通常同时把无线链路也连着，所以电量条目照常显示，本函数
    只提供「该键盘此刻是有线」的事实，供显示层标「(有线)」+ 低电量豁免。

    ── 阻塞策略（2026-10-13）──
    `force=False`（快照/菜单路径）：**非阻塞**。缓存过期就起后台线程去刷，本次
    直接返回上一次的结果（首次为空集）。冷启动那 ~7s 不再堵住快照。
    `force=True`（低电告警前）：同步扫一次 —— 那个场景「插着线还被弹低电」是
    假警报，不能忍，宁可等。
    """
    global _wired_cache, _wired_refreshing
    now = time.monotonic()
    if force:
        _wired_cache = (now, _scan_wired())
        return _wired_cache[1]
    if now - _wired_cache[0] >= _WIRED_TTL and not _wired_refreshing:
        _wired_refreshing = True

        def _bg() -> None:
            global _wired_cache, _wired_refreshing
            try:
                _wired_cache = (time.monotonic(), _scan_wired())
            except Exception as exc:      # noqa: BLE001
                log.debug("wired_bases bg: %s", exc)
            finally:
                _wired_refreshing = False

        threading.Thread(target=_bg, daemon=True).start()
    return _wired_cache[1]


def is_wired(name: str, *, force: bool = False) -> bool:
    """某设备（友好名，可带 2.4G 后缀）此刻是否插着 USB 线。

    `force=True` 绕过 TTL 缓存 —— 低电量通知触发前必须用一次：缓存过期
    一个周期的代价是「插着线还被弹低电通知」，这是假警报，不能忍。
    """
    return base_name(name) in wired_bases(force=force)


def _setupapi():
    return ctypes.windll.setupapi     # type: ignore[attr-defined]


def _hid():
    return ctypes.windll.hid          # type: ignore[attr-defined]


# ★ 结构体与 Win32 签名统一在 win32ctypes.py（唯一真相源）—— 见该模块头注：
#   各模块重复定义 _GUID/_IFD 会让 windll.setupapi 的全局 argtypes 互相覆盖
#   （2026-10-13 实锤）。旧的 _GUID/_IFD/_DETAIL/_ATTRS 与 _invalid() 已迁走。


def enumerate_receivers() -> list[tuple[int, int, str]]:
    """返回本机当前插着的已知接收器 [(vid, pid, 产品串)]。非 Windows 返回 []。"""
    try:
        setupapi = _setupapi()
        hid = _hid()
        kernel32 = ctypes.windll.kernel32      # type: ignore[attr-defined]
    except AttributeError:      # 非 Windows（单测环境）
        return []

    # ★ 签名与结构体一律走共享模块（见 win32ctypes 头注）—— 三模块共用同一套，
    #   杜绝 windll.setupapi 的全局 argtypes 被互相覆盖（2026-10-13 实锤 bug）。
    w32.ensure_argtypes()
    guid = w32.hid_guid()
    hdev = setupapi.SetupDiGetClassDevsW(byref(guid), None, None,
                                         DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == w32.invalid_handle():
        log.debug("SetupDiGetClassDevsW failed: %s", ctypes.get_last_error())
        return []

    found: dict[tuple[int, int], str] = {}
    i = 0
    while True:
        ifd = w32.IFD()
        ifd.cbSize = ctypes.sizeof(w32.IFD)   # 不设就第一条就失败 → 枚举出 0 个
        if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, byref(guid), i,
                                                    byref(ifd)):
            break
        i += 1
        det = w32.DETAIL()
        det.cbSize = 8          # x64 对齐后 cbSize 必须是 8
        size = wintypes.DWORD(ctypes.sizeof(w32.DETAIL))
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, byref(ifd), byref(det), size, byref(size), None):
            continue
        h = kernel32.CreateFileW(det.DevicePath, 0, w32.FILE_SHARE_READ
                                 | w32.FILE_SHARE_WRITE, None, w32.OPEN_EXISTING,
                                 0, 0)
        if not h or h == w32.invalid_handle():
            continue
        try:
            attrs = w32.ATTRS()
            attrs.Size = ctypes.sizeof(w32.ATTRS)
            if not hid.HidD_GetAttributes(h, byref(attrs)):
                continue
            key = (attrs.VendorID, attrs.ProductID)
            if key in KNOWN_RECEIVERS and key not in found:
                buf = ctypes.create_unicode_buffer(256)
                prod = buf.value if hid.HidD_GetProductString(h, buf, 256) else ""
                found[key] = prod
        finally:
            kernel32.CloseHandle(h)
    setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return [(v, p, prod) for (v, p), prod in found.items()]


def devices(bt_names: set[str] | frozenset[str] = frozenset()) -> dict[str, int | None]:
    """{显示名: 电量%|None} —— 只补「没在蓝牙上」的那张键盘。

    读数优先级：`dongle` 的本会话实时帧 > `lastknown` 历史存档。
    键盘被判离线（通道持续静默）时不显示，解决「关机了菜单还在」的老 bug。

    Args:
        bt_names: 当前蓝牙列表里已有的友好名集合。某键盘已在其中，说明它
            此刻走的是蓝牙（实时读数），这里就不再重复添 2.4G 条目。
    """
    from . import dongle, lastknown, wired24

    out: dict[str, int | None] = {}
    for vid, pid, prod in enumerate_receivers():
        base = KNOWN_RECEIVERS.get((vid, pid))
        if not base or base in bt_names:
            continue
        wired = False
        try:
            wired = wired24.plugged(base)
        except Exception:      # noqa: BLE001 - 有线探测失败按未插线处理
            pass
        if dongle.is_offline(base) and not wired:
            log.debug("2.4G %s 持续静默，判离线 -> 不显示", base)
            continue
        live = dongle.battery(base)
        if live is not None:
            pct = live
        elif wired:
            # 关闭档 + 数据线：无线链路全死，只有有线 feature 能读到电量
            pct = wired24.battery(base)
            log.debug("有线 %s -> %s", base, pct)
        else:
            pct = (lastknown.get(base) or (None,))[0]
        out[base + SUFFIX] = pct
        log.debug("2.4G receiver %04X:%04X (%s) -> %s%s", vid, pid, prod, pct,
                  " [实时]" if live is not None else
                  (" [有线]" if wired else " [存档]"))
    return out


def is_stale_entry(name: str) -> bool:
    """该显示名是否来自本模块（2.4G 兜底条目）。"""
    return name.endswith(SUFFIX)


def base_name(name: str) -> str:
    """剥掉连接方式后缀（" (2.4G)" / " (有线)"），返回蓝牙模式下的原始友好名。

    ⚠️ 两种后缀都要剥（2026-10-12 修）：插线时 agent 会把 " (2.4G)" 改写成
    " (有线)"，只剥 2.4G 会让 `base_name("… (有线)")` 原样返回 → 有线判定
    （`wired_bases` 比对）与低电豁免全部失配，插着线还会被弹低电。
    """
    for suf in (SUFFIX, WIRED_SUFFIX):
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def stale_age(name: str) -> float | None:
    """这条 2.4G 读数距今多少秒；不是本模块条目或没记录则返回 None。

    优先用 `dongle` 的实时帧时间（那才是真观测时刻），退回到 lastknown 存档。
    """
    if not is_stale_entry(name):
        return None
    from . import dongle, lastknown
    live = dongle.battery_age(base_name(name))
    return live if live is not None else lastknown.age(base_name(name))


def is_live(name: str) -> bool:
    """这条 2.4G 读数是不是本会话从接收器通道**实时**收到的（而非历史存档）。"""
    if not is_stale_entry(name):
        return False
    from . import dongle
    return dongle.battery_age(base_name(name)) is not None


def is_sleeping(name: str) -> bool:
    """键盘此刻是否在休眠（最后收到的是 `type=02 A=00`）。

    休眠 ≠ 离线：只是省电静默，仍有电、一碰就醒。显示层用来标「休眠中」，
    避免用户以为读数是假的。
    """
    if not is_stale_entry(name):
        return False
    from . import dongle
    return dongle.is_sleeping(base_name(name))


def sleep_age(name: str) -> float | None:
    """进入休眠多久（秒）；非本模块条目或没收到休眠帧返回 None。"""
    if not is_stale_entry(name):
        return None
    from . import dongle
    return dongle.sleep_age(base_name(name))


# ghost_hint() 已删除（2026-10-12 用户拍板）：菜单里不再有任何灰字缺失提示。
# 在线/离线唯一判据 = poll24 扫描（启动/每 5 分钟/点「立即刷新」各扫一遍），
# 不在的设备条目直接隐藏，不给解释文案。
