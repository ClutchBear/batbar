"""Feature-report 通道（2026-10-12 实测驱动的新模块）。

背景：迈从 K87 的 2.4G 接收器（05AC:024F）在 0xFF02 厂商口上「不推不答」
（四轮 probe_query 实测证否，见 README §6-G④），但 `--collections` 全接口
扫描发现另一个 collection（0xFF04/0x0002）有 Feature Report 0x06，纯拉取式
（主机 HidD_GetFeature 同步读，设备不推）：

    ★ ID=0x06  06 00 02 64 64 64 65 92     （2026-10-12 11:12 实测）

⚠️⚠️ 2026-10-12 15:30 现场证伪「这是电量通道」：Hub 网页实时显示 90%，
但本 feature 的 byte[3] 仍恒为 0x64=100；SetFeature 发查询帧后 byte[1]
回显为 0x87/0x4A 且能保持、`64 64 64 65` 纹丝不动 —— 证明这块 feature 是
**主机可读写、设备不主动刷新**的静态配置 buffer，**不是**实时电量传感器。
Hub 的 90% 来自 0xFF02 口对 `13 4a 01 00`（reportID 0x13，2.4G 默认）的
**input 应答帧**（见 README ④-quin/quinn）。本模块现阶段的定位只剩
「枚举 + 采样留证」（`samples()` 供取证分析），电量语义请走 0xFF02 input
通道，勿在此解码。decode_guess 永久返回 None。

⚠️ 2026-10-12 20:43 再修正：**本模块也不再提供「活帧」证据**。曾经把
「拉取成功」转记进 dongle 存活统计（理由：迈从当时不推 input 帧），但实测
键盘关机后这块 buffer 读到的字节一字不变（`06 00 02 64 64 64 65 92`）——
它只证明接收器插着，不证明键盘在线，会导致条目永不消失（用户报「K87 完全
关闭的情况下托盘还显示 2.4G」）。真活帧来源 = 0xFF02 input：键盘在 2.4G 上
的推送帧 + `poll24` 代发官方 getBattery 得到的应答帧。

与 0xFF02 监听的关系：HidD_GetFeature 是同步 IOCTL，**不消耗输入队列**，
与 dongle 的单读者监听互不冲突（不同 collection、不同机制），可放心并存。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import byref, create_string_buffer, wintypes

from . import win32ctypes as w32

log = logging.getLogger("batbar.feature24")

POLL_INTERVAL = 30.0          # 秒。feature 是拉取式，无事件可依，定时轮询
BRUTE_IDS = range(0x20)       # 首次发现时穷举的 reportID 范围


# ---------- Win32 绑定（统一走 win32ctypes，勿在此重声明 argtypes） ----------

def _dlls():
    """返回 (setupapi, hid, kernel32)；非 Windows 抛 AttributeError。"""
    return (ctypes.windll.setupapi,          # type: ignore[attr-defined]
            ctypes.windll.hid,               # type: ignore[attr-defined]
            ctypes.windll.kernel32)          # type: ignore[attr-defined]


def _feature_collections(vid: int, pid: int) -> list[tuple[str, int]]:
    """枚举目标接收器**声明了 feature report** 的接口。

    Returns [(device_path, FeatureReportByteLength), ...]；非 Windows 返回 []。

    ⚠️ 2026-10-13：本函数原先自持一份 `_GUID`/`_IFD`/`_DETAIL`/`_CAPS` 并自行
    声明 argtypes，与 dongle/receiver24/wired24 争夺同一批进程级 windll 函数，
    **最后设置者赢** ⇒ 别的模块拿自己的结构体调用就抛
    `TypeError: expected LP__IFD instance instead of pointer to _IFD`
    （表现为 2.4G 条目悄悄消失）。现统一改用 `win32ctypes`（唯一真相源）。
    """
    try:
        setupapi, hid, k32 = _dlls()
    except AttributeError:
        return []
    w32.ensure_argtypes()
    guid = w32.hid_guid()
    hdev = setupapi.SetupDiGetClassDevsW(byref(guid), None, None,
                                         w32.DIGCF_PRESENT | w32.DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == w32.invalid_handle():
        return []
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    try:
        i = 0
        while True:
            ifd = w32.IFD()
            ifd.cbSize = ctypes.sizeof(w32.IFD)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, byref(guid),
                                                        i, byref(ifd)):
                break
            i += 1
            det = w32.DETAIL()
            det.cbSize = 8
            size = wintypes.DWORD(ctypes.sizeof(w32.DETAIL))
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, byref(ifd), byref(det), size, byref(size), None):
                continue
            up = det.DevicePath.upper()
            if f"VID_{vid:04X}" not in up or f"PID_{pid:04X}" not in up:
                continue
            h = k32.CreateFileW(det.DevicePath, 0,
                                w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE,
                                None, w32.OPEN_EXISTING, 0, 0)
            if not h or h == w32.invalid_handle():
                continue
            try:
                pp = ctypes.c_void_p()
                if not hid.HidD_GetPreparsedData(h, byref(pp)):
                    continue
                caps = w32.CAPS()
                hid.HidP_GetCaps(pp, byref(caps))
                hid.HidD_FreePreparsedData(pp)
                if caps.FeatureReportByteLength > 0 and det.DevicePath not in seen:
                    seen.add(det.DevicePath)
                    out.append((det.DevicePath, caps.FeatureReportByteLength))
            finally:
                k32.CloseHandle(h)
        return out
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)


def read_feature(path: str, featlen: int, rid: int) -> bytes | None:
    """同步拉一个 feature report（首字节预置 reportID）。失败返回 None。

    对不存在的 ID，HidD_GetFeature 立即失败、无副作用 —— 穷举安全。
    """
    try:
        _s, hid, k32 = _dlls()
    except AttributeError:
        return None
    w32.ensure_argtypes()
    # 探针实测用 GENERIC_READ|GENERIC_WRITE 打开成功；只读是否足够未验证，
    # 沿用已证实的组合（GetFeature 是同步 IOCTL，不消耗输入队列，无单读者冲突）。
    h = k32.CreateFileW(path, 0xC0000000,
                        w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE,
                        None, w32.OPEN_EXISTING, 0, 0)
    if not h or h == w32.invalid_handle():
        return None
    try:
        buf = create_string_buffer(featlen)
        buf[0] = bytes([rid])
        ctypes.set_last_error(0)
        if hid.HidD_GetFeature(h, buf, featlen):
            return bytes(buf.raw[:featlen])
        return None
    finally:
        k32.CloseHandle(h)


# ---------- 发现 + 轮询状态机 ----------

_lock = threading.Lock()
_found: dict[str, list[tuple[str, int, int]]] = {}   # name -> [(path, featlen, rid)]
_samples: dict[str, list[tuple[float, bytes]]] = {}  # name -> [(ts, raw)] 最近 N 组
_stop = threading.Event()
_thread: threading.Thread | None = None
MAX_SAMPLES = 40


def discover(name: str, vid: int, pid: int) -> list[tuple[str, int, int]]:
    """首次发现：对目标的所有 feature 接口穷举 ID，返回成功的
    [(path, featlen, rid)]。结果缓存（设备重插需 clear_cache）。"""
    hits: list[tuple[str, int, int]] = []
    for path, featlen in _feature_collections(vid, pid):
        for rid in BRUTE_IDS:
            data = read_feature(path, featlen, rid)
            if data is not None:
                hits.append((path, featlen, rid))
                log.info("feature 命中 %s rid=0x%02x len=%d: %s",
                         name, rid, featlen, data.hex(" "))
    with _lock:
        _found[name] = hits
    return hits


def poll_once(name: str) -> list[bytes]:
    """拉一轮已发现的 feature report，存入样本环形缓冲。"""
    with _lock:
        targets = list(_found.get(name, []))
    got: list[bytes] = []
    for (path, featlen, rid) in targets:
        data = read_feature(path, featlen, rid)
        if data is not None:
            got.append(data)
            with _lock:
                buf = _samples.setdefault(name, [])
                buf.append((time.time(), data))
                if len(buf) > MAX_SAMPLES:
                    del buf[:len(buf) - MAX_SAMPLES]
    return got


def samples(name: str) -> list[tuple[float, bytes]]:
    with _lock:
        return list(_samples.get(name, []))


def clear_cache() -> None:
    """设备重插/换接收器后调用，强制重新发现。"""
    with _lock:
        _found.clear()
        _samples.clear()


def _loop() -> None:
    """后台轮询：对 TARGETS 里每个目标，没发现过先发现，发现过就定时拉。

    ⚠️ **不再把「拉取成功」当活帧转记**（2026-10-12 20:43 修，用户报
    「K87 完全关闭的情况下托盘还显示 2.4G」）：这块 feature buffer 在**接收器**
    上，键盘关机/休眠时照样读得到同样的静态字节（实测 20:37、20:42 日志里
    `06 00 02 64 64 64 65 92` 一字不变）——「读得到」只证明**接收器插着**，
    完全不证明键盘在线。旧的转记逻辑会让 K87 永远不判离线，条目挂着 lastknown
    的 100% 假装在线。真正的活帧来源是 0xFF02 input 通道：键盘在 2.4G 上时的
    事件推送帧 + `poll24` 代发官方 getBattery 查询得到的**应答帧**。
    """
    from .dongle import TARGETS
    while not _stop.is_set():
        for (vid, pid), name in list(TARGETS.items()):
            try:
                with _lock:
                    known = name in _found
                if not known:
                    hits = discover(name, vid, pid)
                    if not hits:
                        continue          # 该目标无 feature 口，下轮再试
                poll_once(name)           # 只存样本供取证，不转记活帧
            except Exception as exc:      # noqa: BLE001 - 单设备失败不拖垮循环
                log.debug("feature poll %s: %s", name, exc)
        _stop.wait(POLL_INTERVAL)


def start() -> None:
    """起后台轮询线程（幂等）。托盘在 run() 里调一次即可。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="feature24", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def decode_guess(raw: bytes) -> int | None:
    """**永久返回 None** —— 2026-10-12 15:30 现场证伪本 feature 非电量通道。

    Hub 实时 90% 时本帧 byte[3] 仍恒 0x64=100，且 SetFeature 往返只改到
    byte[1] 回显位（见模块 docstring 与 README ④-quinn）。`64 64 64 65` 是
    静态配置 buffer，不是电量。真电量走 0xFF02 input 应答帧，勿在此解码。
    """
    return None
