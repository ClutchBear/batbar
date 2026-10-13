"""有线（USB 数据线）通道：插线键盘的识别与电量读取（2026-10-12 新增）。

── 为什么需要它 ──
键盘背面三档开关（蓝牙 / 关闭 / 2.4G）× 插数据线，共有三种组合：

  1. 蓝牙档 + 线：BT 链路照样报电量，插线只相当于充电；
  2. **关闭档 + 线**：无线链路全死 —— 接收器收不到任何帧，蓝牙也不在连，
     旧实现里这张键盘会**彻底消失**（receiver24 判离线就隐藏）。只有走 USB
     有线通道才能拿到它的电量；
  3. 2.4G 档 + 线：2.4G 推送帧照常（与有线读数互证，实测同刻都是 90%）。

── 实测（2026-10-12，MCHOSE K87 拨 2.4G 档 + 插线）──
· 插线后键盘**本体也枚举成 USB 设备**，而且**与接收器共用 VID/PID**
  （`05AC:024F`）—— 只能靠产品串区分：
      接收器 = "2.4G Wireless Receiver"
      键盘   = "MCHOSE K87 Gaming Keyboard"
  这正是旧的「有线判定」（要求出现非接收器 PID 的 USB 节点）失效的原因。
· 键盘自带的 **520 字节 feature 接口**（usage page 0xFF00）支持有线 getBattery：
      SetFeature: 06 87 00 00 01 00 02 00 (+ 补零到 520)
      GetFeature: 06 87 00 00 01 00 02 00 5a 10 …
                                        └ [8]=电量%(0x5a=90)  [9]=充电标志 0x10
  与 gx87-studio 逆出的 SinoWealth 有线协议逐字节一致（HeaderSize=8、
  payload[0]=电量、payload[1] 高 4 位=充电中 / 低 4 位=充满）。实测同刻
  2.4G 也报 90%，两条链路互证。

── 与其它模块的关系 ──
· 只读式拉取（SetFeature 查询 + GetFeature 读回），不改设备状态；
· 结果独立缓存（**不混进 dongle 的 2.4G 帧统计**，否则 tooltip 会把有线读数
  说成「2.4G 实时」）；由 receiver24/agent 显式取用；
· 拔出线/设备消失 → `plugged()` 立刻转 False，条目按原逻辑回落。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import POINTER, byref, wintypes

from . import win32ctypes as w32

log = logging.getLogger("batbar.wired")

# 有线 getBattery 查询帧（bundle 逆向 + gx87-studio 双证；报告 ID 0x06 打头）
WIRED_QUERY = bytes([0x06, 0x87, 0x00, 0x00, 0x01, 0x00, 0x02, 0x00])
# 有线通道的 feature 报告长度下限：接收器那些口最多 8~65 字节，键盘这个是 520
_MIN_FEATURE = 256
POLL_INTERVAL = 30.0          # 有线读数轮询间隔（插线时才有意义，成本极低）
_RECEIVER_HINTS = ("receiver", "dongle", "接收器")


# ★ 结构体与 Win32 签名统一在 win32ctypes.py（唯一真相源）—— 见该模块头注。
#   旧写法（本模块 2026-10-12 单独用 c_void_p 规避 argtypes 互相覆盖）已收敛为
#   共享声明，_GUID/_IFD/_DETAIL/_ATTRS/_HIDP_CAPS 与 _invalid() 全部迁走。


def _setupapi():
    return ctypes.windll.setupapi          # type: ignore[attr-defined]


def _hid():
    return ctypes.windll.hid               # type: ignore[attr-defined]


def _declare(sa, hid, k32) -> None:
    """所有 HID / SetupAPI 函数签名统一由共享模块声明（幂等）。

    ⚠️ 历史坑：`ctypes.windll.hid` / `setupapi` 是**进程级共享对象**，
    argtypes 是全局状态。三个模块各自 `xxx.argtypes = [...]` 会互相覆盖，
    导致 ctypes 类型校验失败 → 2.4G 条目整条消失。现在统一走
    `win32ctypes.ensure_argtypes()`（指针一律 c_void_p），从根上消除竞争。
    """
    w32.ensure_argtypes()


def _caps_feature_len(hid, h) -> int:
    pp = ctypes.c_void_p()
    if not hid.HidD_GetPreparsedData(h, byref(pp)) or not pp:
        return 0
    try:
        caps = w32.CAPS()
        if hid.HidP_GetCaps(pp, byref(caps)) < 0:
            return 0
        return int(caps.FeatureReportByteLength)
    finally:
        hid.HidD_FreePreparsedData(pp)


def enumerate_wired(targets: dict[tuple[int, int], str]) -> list[dict]:
    """找「键盘本体插线」暴露出来的有线 feature 接口。

    Args:
        targets: {（vid,pid): 友好名}，通常就是 dongle.TARGETS。
    Returns:
        [{"name", "path", "featlen", "product"}...] —— 每个插线的键盘一条。
    判据：① VID/PID 命中目标；② 产品串**不像接收器**（"2.4G Wireless Receiver"
    会被排除）；③ 该接口的 FeatureReportByteLength ≥ 256（有线厂商通道特征）。
    非 Windows 返回 []。
    """
    try:
        sa, hid, k32 = _setupapi(), _hid(), ctypes.windll.kernel32  # type: ignore[attr-defined]
    except AttributeError:
        return []
    _declare(sa, hid, k32)

    guid = w32.hid_guid()
    hid.HidD_GetHidGuid(byref(guid))
    hdev = sa.SetupDiGetClassDevsW(byref(guid), None, None,
                                   w32.DIGCF_DEVICEINTERFACE | w32.DIGCF_PRESENT)   # PRESENT|DEVICEINTERFACE
    if not hdev or hdev == w32.invalid_handle():
        return []
    out: list[dict] = []
    seen: set[str] = set()
    try:
        i = 0
        while True:
            ifd = w32.IFD()
            ifd.cbSize = ctypes.sizeof(w32.IFD)
            if not sa.SetupDiEnumDeviceInterfaces(hdev, None, byref(guid), i,
                                                  byref(ifd)):
                break
            i += 1
            det = w32.DETAIL()
            det.cbSize = 8
            size = wintypes.DWORD(ctypes.sizeof(w32.DETAIL))
            if not sa.SetupDiGetDeviceInterfaceDetailW(hdev, byref(ifd),
                                                       byref(det), size,
                                                       byref(size), None):
                continue
            h = k32.CreateFileW(det.DevicePath, 0, w32.FILE_SHARE_READ
                                 | w32.FILE_SHARE_WRITE, None,
                                 w32.OPEN_EXISTING, 0, 0)
            if not h or h == w32.invalid_handle():
                continue
            try:
                attrs = w32.ATTRS()
                attrs.Size = ctypes.sizeof(w32.ATTRS)
                if not hid.HidD_GetAttributes(h, byref(attrs)):
                    continue
                key = (attrs.VendorID, attrs.ProductID)
                name = targets.get(key)
                if name is None or key in seen:
                    continue
                buf = ctypes.create_unicode_buffer(256)
                prod = (buf.value
                        if hid.HidD_GetProductString(h, buf, 512) else "")
                low = prod.lower()
                if any(hint in low for hint in _RECEIVER_HINTS):
                    continue                    # 这是接收器，不是键盘本体
                featlen = _caps_feature_len(hid, h)
                if featlen < _MIN_FEATURE:
                    continue                    # 不是有线厂商通道
                seen.add(key)
                out.append({"name": name, "path": det.DevicePath,
                            "featlen": featlen, "product": prod})
            finally:
                k32.CloseHandle(h)
    finally:
        sa.SetupDiDestroyDeviceInfoList(hdev)
    return out


def parse_wired(data: bytes) -> tuple[int, bool] | None:
    """解析有线 getBattery 应答 → (电量%, 是否充电中)；不像应答返回 None。

    帧形：`06 87 00 00 01 00 02 00 <pct> <flags> …`（实测）。
    flags 两个半字节（gx87-studio 逆出 + 本机 21:40 探针坐实）：
      · 高 4 位 = **充电中**（0x10 = 正在充，实测关机档 pct=0 时回 `00 10`）
      · 低 4 位 = **充满** —— 两者都算「插着线」（图标 ⚡ + 低电豁免），
        这是既有的产品契约，别改成只认高 4 位。
    ⚠️ pct=0 是**真实读数**（电池耗尽）：有线是查询-应答式，不存在 2.4G
    推送链路那种「切档瞬间吐假 0」的毛刺，显示层不得把有线条目的 0 过滤掉。
    """
    if len(data) < 10 or data[0] != 0x06 or data[1] != 0x87:
        return None
    pct = data[8]
    if not 0 <= pct <= 100:
        return None
    flags = data[9]
    charging = bool(flags & 0xF0) or bool(flags & 0x0F)
    return pct, charging


def read_wired(path: str, featlen: int) -> tuple[int, bool] | None:
    """向有线 feature 接口发一次查询并读回 (电量%, 充电中)；失败返回 None。"""
    try:
        hid, k32 = _hid(), ctypes.windll.kernel32     # type: ignore[attr-defined]
    except AttributeError:
        return None
    h = k32.CreateFileW(path, 0xC0000000, 0x03, None, 3, 0, 0)   # 同步句柄
    if not h or h == w32.invalid_handle():
        return None
    try:
        q = (WIRED_QUERY + bytes(featlen))[:featlen]
        sb = ctypes.create_string_buffer(q, featlen)
        if not hid.HidD_SetFeature(h, sb, featlen):
            return None
        time.sleep(0.05)          # 给固件一点时间把答案写进 feature
        gb = ctypes.create_string_buffer(featlen)
        gb[0] = bytes([WIRED_QUERY[0]])
        if not hid.HidD_GetFeature(h, gb, featlen):
            return None
        return parse_wired(bytes(gb.raw[:featlen]))
    except Exception as exc:      # noqa: BLE001 - 读失败按「无数据」处理
        log.debug("wired read failed: %s", exc)
        return None
    finally:
        k32.CloseHandle(h)


# ---------- 状态缓存 + 后台轮询 ----------
_state: dict[str, dict] = {}          # 名 -> {path, featlen, pct, charging, ts}
_lock = threading.Lock()
_thread: threading.Thread | None = None
_running = False


def poll_once() -> dict[str, tuple[int, bool] | None]:
    """扫一次：找出插线的键盘并读电量，返回 {名: (pct, charging)|None}。

    枚举目标 = `TARGETS ∪ WIRED_TARGETS`（2026-10-12）：迈从 K87 有线与
    接收器同 VID/PID（在 TARGETS 里靠产品串区分），LEOBOG 有线是**独立**
    的 258A:010C（在 WIRED_TARGETS 里）。
    """
    from .dongle import TARGETS, WIRED_TARGETS
    found = enumerate_wired({**TARGETS, **WIRED_TARGETS})
    res: dict[str, tuple[int, bool] | None] = {}
    now = time.time()
    with _lock:
        alive = {f["name"] for f in found}
        for stale in set(_state) - alive:      # 拔线/换档 → 立刻失效
            del _state[stale]
        for f in found:
            got = read_wired(f["path"], f["featlen"])
            res[f["name"]] = got
            _state[f["name"]] = {"path": f["path"], "featlen": f["featlen"],
                                 "pct": got[0] if got else None,
                                 "charging": got[1] if got else False,
                                 "ts": now}
    return res


def plugged(name: str) -> bool:
    """该键盘此刻是否插着数据线（有线 feature 接口在枚举里）。"""
    with _lock:
        return name in _state


def battery(name: str) -> int | None:
    """有线通道最近一次读到的电量（None = 没读到/没插线）。

    ⚠️ 「0% + 充电中」按 None 处理（2026-10-12 实测）：三档开关拨**关闭**
    只插线时，键盘主控不上电、电量计固定回 0（探针实测 `…00 10`，多延迟
    重读恒定）—— 这不是真实电量，显示「0%」是误导（用户报「显示电量为0」）。
    菜单会显示 "--"（2026-10-13 起不再用 "?"），tooltip 标明是关机充电态。
    真把电池耗干再插线同样显示 "--"，代价可接受：关机充电时用户要的不是
    读数，是「它在充」这件事（⚡）。
    """
    with _lock:
        st = _state.get(name)
        if not st:
            return None
        pct = st["pct"]
        if pct == 0 and st["charging"]:
            return None
        return pct


def charging(name: str) -> bool:
    """该键盘此刻是否在充电（有线通道的充电标志位）。"""
    with _lock:
        st = _state.get(name)
        return bool(st and st["charging"])


def snapshot() -> dict[str, dict]:
    """调试/测试用：当前有线状态快照。"""
    with _lock:
        return {k: dict(v) for k, v in _state.items()}


def _loop() -> None:
    while _running:
        try:
            poll_once()
        except Exception as exc:      # noqa: BLE001 - 轮询失败不影响其它源
            log.debug("wired poll: %s", exc)
        for _ in range(int(POLL_INTERVAL * 2)):
            if not _running:
                return
            time.sleep(0.5)


def start(interval: float | None = None) -> None:
    """启动后台轮询（幂等）。interval 秒一轮，默认 30s。"""
    global _thread, _running, POLL_INTERVAL
    if interval:
        POLL_INTERVAL = float(interval)
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True, name="wired24")
    _thread.start()


def stop() -> None:
    global _running
    _running = False


def reset() -> None:
    """仅测试用：清空缓存。"""
    with _lock:
        _state.clear()
