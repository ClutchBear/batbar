"""2.4G 接收器厂商通道的**被动监听器** —— 拿到键盘的实时电量与在离线状态。

── 背景（本机实测，2026-10-12） ──
之前 README「坑 8」的结论是「2.4G 下拿不到实时电量」，那是**错的**：当时
`CreateFileW` 只申请了 `GENERIC_READ`，句柄没有写权限，导致所有探测帧写失败，
于是误判为「无通道」。本模块只做**只读**监听（本模块自己不写一个字节），
实测能收到键盘经由接收器转发上来的事件帧。周期性主动查询由 poll24 负责
（独立只写句柄，不碰本模块独占的输入队列）。

目标接口（实测路径）：

    \\\\?\\hid#vid_3554&pid_fa09&mi_01&col01#...     ← LB Hi98C Pro 的 2.4G 接收器
    \\\\?\\hid#vid_05ac&pid_024f&mi_01&col01#...     ← MCHOSE K87 的 2.4G 接收器
    usagePage = 0xFF02, InputReportByteLength = 20

── 多接收器（2026-10-12 新增迈从 K87） ──
迈从 K87 的 2.4G 接收器 VID/PID = `05AC:024F`（借用 Apple VID，与其蓝牙
形态同号）。本机枚举证实两颗接收器**collection 结构逐项同构**（MI_01 下
COL01 供应商定义 / COL02 用户控制 / COL03 系统控制器 / COL04 键盘 /
COL05 鼠标 / COL06 供应商定义，连实例编号模式都相同）——同源公版接收器
方案，与 device-battery-tray 逆出的迈从 K99 V2（`13 4A 01 00…5E`）同源。
⚠️ 帧格式对迈从**尚未实测**（parse_frame 的 `13 0a 01 00 04` 判据是按
LEOBOG 抓包写的）；若迈从帧格式不同，收不到帧时按「新目标」再加一套
判别，别改现有判据（会破坏 LEOBOG）。

── 帧格式（逐帧 sum8 校验通过） ──

    13 0a 01 00 04 <type> <A> <B> 00×11 <chk>
                                          ↑ 前 8 字节求和 & 0xFF

| type | A              | 含义（本机对照时间戳确认）        |
|------|----------------|----------------------------------|
| 0x02 | 01 / 00        | **休眠 ↔ 唤醒**上报（见下方证据链） |
| 0x05 | 0x5c/5e/5d=92/94/93 | **电量**（无滤波 ADC，±2 抖动）      |
| 0x07 | 00             | 握手/状态，暂不明                  |

`type=02` 到底是不是「背光超时」？2026-10-12 用 `tools/keybeat.py` 做了
一次**改参数对照实验**，坐实它是**设备休眠**上报，不是背光超时：

  · 驱动「休眠」默认（约 180s）时：停手 → `02 00` 延迟 **~186s**；
  · 用户把滑块改成 **30s** 后：停手 → `02 00` 延迟 **26.1s**。

延迟跟着设定值走（差值在人为反应时间内），因果锁定。同一轮还确认：
电量帧只在敲键后出现（停手后不再有），即**活动驱动**上报。

── 由此得到的两条能力 ──
1. **实时电量**：键盘被使用时会反复推 type 05，跟随使用即可拿到当前值；
   停手静默后不再有 —— 活动驱动上报，不是周期上报。
2. **在/离线判据**：这是**事件驱动**通道 —— 键盘关机后**彻底零流量**。
   所以「持续静默」可以判定离线。

── 产品决策（2026-10-12 用户拍板，同日 21:00 改为**扫描制**） ──
**休眠与关机同权，都算「不在」，条目自动消失** —— 对齐官方 LEOBOG 驱动
的行为（键盘一睡就回「正在查找设备」页）。理由：托盘上的旧读数没有价值，
用户宁可要一个诚实的空状态。

判定不再靠「静默多久」（观察窗口）而是靠**扫描**：

  · 扫描 = 逐个目标发官方 getBattery 查询帧，等 1.5s 看有没有应答
    （`poll24.scan()`）；**启动扫一遍、之后每 5 分钟一遍、点「立即刷新」
    再扫一遍**；
  · 最近一次扫描没应答 → 不在，条目隐藏；还没扫过 → 先按不在（启动后
    ~2s 内就出结论，不会像旧设计那样「先显示 90s 再消失」）；
  · 收到 `type=02 A=0`（休眠帧）→ 立即隐藏（键盘亲口说的，不用等扫描）；
  · 任何时候来一帧（敲键唤醒 / 扫描应答）→ 立即复活。

⚠️ 旧设计（已删）用「持续静默 offline_after()=90s + 观察期保护」推断离线，
两个毛病：① 开机后 90s 内不管在不在都显示；② 需要 feature24 转记假活帧
才能不被误杀（那个假设已被证伪）。扫描制直接问设备本人，判据单一。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import POINTER, byref, wintypes

from . import win32ctypes as w32

log = logging.getLogger("batbar.dongle")

# 已知的 2.4G 接收器 -> 该键盘在蓝牙模式下的友好名（须与 bt_battery.friendly()
# 的结果一致，用户才认得出是同一张键盘）。这里作为**唯一数据源**，
# receiver24.KNOWN_RECEIVERS 直接引用它。
TARGETS: dict[tuple[int, int], str] = {
    (0x3554, 0xFA09): "键盘 LEOBOG Hi98C Pro",
    (0x05AC, 0x024F): "键盘 迈从 K87",   # K87 的 2.4G 接收器（与其 BLE 节点同 VID/PID）
}

# 有线（USB 数据线）形态专属 VID/PID —— 2026-10-12 22:10 探针实测：
# LEOBOG 插线后键盘本体枚举成 `258A:010C "Gaming Keyboard"`（与它蓝牙/
# 接收器形态的 3554:FA0x **完全不同**），带 520B feature 接口，吃迈从同款
# SinoWealth 协议（`06 87` 查询 → `…64 10` = 100% + 充电位）。
# 迈从 K87 不在这张表里：它有线与接收器**共用** 05AC:024F，靠产品串区分
# （见 wired24._RECEIVER_HINTS）。wired24 枚举时用 TARGETS ∪ WIRED_TARGETS。
WIRED_TARGETS: dict[tuple[int, int], str] = {
    (0x258A, 0x010C): "键盘 LEOBOG Hi98C Pro",
}

FRAME_LEN = 20
_MAGIC = bytes((0x13, 0x0A, 0x01, 0x00, 0x04))

T_BACKLIGHT = 0x02      # A: 1=灯亮 0=灯灭（用户目击确认）
T_BATTERY = 0x05        # A: 电量%（无滤波 ADC，实测在真值 ±2 内抖动）
T_HANDSHAKE = 0x07      # A: 恒 0，用途不明


def parse_frame(data: bytes) -> tuple[str, int] | None:
    """解析一帧，返回 `("battery", 电量%) | ("backlight", 0|1) | ("other", A)`。

    帧头不匹配或 sum8 校验失败返回 None（这正是万能/vendor 通道混来的噪声
    数据的过滤器，很重要 —— 本集合偶尔会漏进标准 HID 报文）。

    2026-10-12 新增第二帧形：**命令应答帧**（poll24 发查询后设备回来）——

        13 4a 01 00 02 <pct> <chg> 00×12 <chk>      ← 16:56 实抓两笔（93%）

    与通知帧（`13 0a …`）的区别在第 2 字节：0x4a = getBattery 命令回显。
    sum8 校验含首字节 0x13（实抓帧验算：13+4a+01+00+02+5d+01 = 0xBE ✓）。
    """
    if len(data) < FRAME_LEN:
        return None
    if bytes(data[:5]) == _MAGIC:
        if (sum(data[:8]) & 0xFF) != data[FRAME_LEN - 1]:
            log.debug("帧校验失败: %s", bytes(data[:FRAME_LEN]).hex(" "))
            return None
        t, a = data[5], data[6]
        if t == T_BATTERY:
            return ("battery", a) if 0 <= a <= 100 else None
        if t == T_BACKLIGHT:
            return ("backlight", 1 if a else 0)
        if t == T_HANDSHAKE:
            return ("handshake", a)
        return ("other", a)
    # 命令应答帧形：[0]=0x13(reportID/magic) [1]=0x4a(getBattery 回显)
    # [2]=total [3]=current [4]=0x02(数据类型) [5]=电量% [6]=充电标志
    if (data[0] == 0x13 and data[1] == 0x4A and data[2] == 0x01
            and data[4] == 0x02):
        if (sum(data[:8]) & 0xFF) != data[FRAME_LEN - 1]:
            return None
        a = data[5]
        return ("battery", a) if 0 <= a <= 100 else None
    return None


# ──────────────────────── 只读监听线程 ────────────────────────

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()

# 每个已识别设备一份状态（key = 友好名）
_stats: dict[str, dict] = {}

# 每个已识别设备的 0xFF02 接口设备路径（_open_targets 枚举时顺手记录，
# 给 poll24 的只写句柄用 —— 免得它再抄一遍 40 行 setupapi 枚举代码）。
_paths: dict[str, str] = {}


# ★ 结构体与 Win32 签名统一在 win32ctypes.py（唯一真相源）—— 见该模块头注：
#   三个模块各定义一份 _GUID/_IFD 会让 windll.setupapi 的全局 argtypes 互相
#   覆盖，表现为「2.4G 条目悄悄消失」。旧的 _GUID/_IFD/_DETAIL/_ATTRS/_CAPS
#   与 _invalid() 已迁走，此处不再重复定义。


def _new_stat() -> dict:
    return {"battery": None, "battery_ts": None, "frame_ts": None,
            "sleeping": False, "sleep_ts": None}


def _open_targets() -> list[tuple[int, str]]:
    """枚举**所有**已知接收器的 0xFF02 厂商输入接口，返回 [(handle, 友好名)]。

    找不到返回空列表。**只读打开**（GENERIC_READ），不申请写权限 ——
    我们不往设备写任何东西，避免打扰它。

    多目标（2026-10-12 新增）：迈从 K87 接收器加进来后，本函数不再
    「见一个就返回」。⚠️ 单读者原则粒度是**每个 collection**：同一
    collection 只能有一个句柄在读（多句柄互抢帧，实测两边都收不全）；
    不同接收器是不同 collection，各读各的互不干扰 —— 所以这里对每个
    目标各开一个句柄是安全的，但调用方必须保证同一目标不重开（见 _watch）。
    """
    setupapi = ctypes.windll.setupapi         # type: ignore[attr-defined]
    hid = ctypes.windll.hid                   # type: ignore[attr-defined]
    k32 = ctypes.windll.kernel32              # type: ignore[attr-defined]

    # ★ 签名一律走共享模块（幂等）—— 三模块共用同一套声明，杜绝 argtypes
    #   互相覆盖（2026-10-13 黑盒冒烟测试抓到的真 bug，见 win32ctypes 头注）。
    w32.ensure_argtypes()
    guid = w32.hid_guid()
    hdev = setupapi.SetupDiGetClassDevsW(byref(guid), None, None,
                                         w32.DIGCF_PRESENT | w32.DIGCF_DEVICEINTERFACE)
    if not hdev or hdev == w32.invalid_handle():
        return []

    out: list[tuple[int, str]] = []
    seen: set[str] = set()                  # 同一目标多个路径只留第一个句柄
    i = 0
    while True:
        ifd = w32.IFD()
        ifd.cbSize = ctypes.sizeof(w32.IFD)   # 不设 → 第一条就枚举失败
        if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, byref(guid), i,
                                                    byref(ifd)):
            break
        i += 1
        det = w32.DETAIL()
        det.cbSize = 8                        # x64 对齐后必须是 8
        size = wintypes.DWORD(ctypes.sizeof(w32.DETAIL))
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, byref(ifd), byref(det), size, byref(size), None):
            continue
        up = det.DevicePath.upper()
        hit = None
        for (vid, pid), name in TARGETS.items():
            if f"VID_{vid:04X}" in up and f"PID_{pid:04X}" in up:
                hit = name
                break
        if not hit:
            continue
        # 只读打开 —— 绝不申请写权限，本模块不往设备写一个字节
        h = k32.CreateFileW(det.DevicePath, w32.GENERIC_READ,
                            w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE, None,
                            w32.OPEN_EXISTING, 0, 0)
        if not h or h == w32.invalid_handle():
            continue
        pp = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(h, byref(pp)):
            k32.CloseHandle(h)
            continue
        caps = w32.CAPS()
        hid.HidP_GetCaps(pp, byref(caps))
        hid.HidD_FreePreparsedData(pp)
        if caps.UsagePage == 0xFF02 and caps.InputReportByteLength >= FRAME_LEN:
            if hit in seen:                 # 同目标第二条第路径：句柄还回去
                k32.CloseHandle(h)
                continue
            seen.add(hit)
            out.append((h, hit))
            with _lock:
                _paths[hit] = det.DevicePath
            continue                        # 不 break：继续枚举下一个目标
        k32.CloseHandle(h)
    setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return out


def _touch(name: str) -> None:
    """接到设备（句柄打开成功）就在统计表里登记一台，让它在 `_stats` 里
    占位（离线判据只看 poll24 扫描结论，这里不负责在线/离线，只保证设备
    「被看见」，frame_ts 初始为 None）。"""
    with _lock:
        _stats.setdefault(name, _new_stat())


def _record(name: str, kind: str, value: int) -> None:
    """收到一帧就更新状态；电量帧顺手写进 lastknown 持久化。"""
    now = time.time()
    with _lock:
        st = _stats.setdefault(name, _new_stat())
        st["frame_ts"] = now
        # 休眠/唤醒状态：type=02 是唯一权威来源（A=0 休眠、A=1 唤醒）。
        # 电量帧只在键盘活动时出现（keybeat 实测），所以收到它也说明醒着。
        if kind == "backlight":
            st["sleeping"] = (value == 0)
            st["sleep_ts"] = now
        elif kind == "battery":
            st["sleeping"] = False
    if kind == "battery":
        with _lock:
            st["battery"] = value
            st["battery_ts"] = now
        try:
            from . import lastknown
            lastknown.put(name, value)
        except Exception as exc:              # noqa: BLE001 - 存档失败不影响主链路
            log.debug("lastknown put %s failed: %s", name, exc)
        log.debug("2.4G 实时电量 %s = %d%%", name, value)
    for cb in _frame_cbs:                     # 帧到 → 让 UI 立即重算（电量/休眠切换）
        try:
            cb(name, kind, value)
        except Exception as exc:              # noqa: BLE001 - 回调失败不影响收帧
            log.debug("dongle frame cb: %s", exc)


# UI 层注册的回调（agent 用它做到帧即时刷新，见 agent.run）
_frame_cbs: list = []


def on_frame(cb) -> None:
    """注册回调 cb(name, kind, value)，每收到一帧同步调用一次。"""
    _frame_cbs.append(cb)


def _watch_one(handle, name: str) -> None:
    """盯一个已打开的接收器接口：阻塞读，掉线退出（每设备一个线程）。

    句柄由调度线程 _watch 转交，本函数退出时负责关句柄 + 清 _stats。
    """
    k32 = ctypes.windll.kernel32              # type: ignore[attr-defined]
    w32.ensure_argtypes()                     # 幂等；保证 ReadFile/CloseHandle 签名已声明
    # ★ 单读者原则：HID 每个 collection 只有一条输入队列，多个句柄会
    #   互相抢帧（实测：两个监听器并存时两边都收不全）。同一目标的句柄
    #   全程只有本线程持有这一个（调度线程的重枚举结果会立刻关掉多余的）。
    try:
        buf = (ctypes.c_ubyte * 512)()
        n = wintypes.DWORD(0)
        while not _stop.is_set():
            ok = k32.ReadFile(handle, buf, 512, byref(n), None)
            if not ok:
                log.debug("ReadFile err=%s（设备拔出？）", k32.GetLastError())
                break
            if n.value:
                got = parse_frame(bytes(buf[:n.value]))
                if got:
                    _record(name, got[0], got[1])
    except Exception as exc:                  # noqa: BLE001
        log.debug("dongle read loop: %s", exc)
    finally:
        try:
            k32.CloseHandle(handle)
        except Exception:                     # noqa: BLE001
            pass
        with _lock:
            _stats.pop(name, None)            # 断开后清干净，避免拿旧状态判离线
        log.info("2.4G 监听结束: %s", name)


def _watch() -> None:
    """调度线程：周期枚举所有已知接收器，为新出现的设备各起一个监听线程。

    热插拔：设备拔出时，对应 _watch_one 的 ReadFile 失败退出、_stats 清掉；
    下轮枚举重新发现该目标，再起新线程（新句柄）——自愈闭环。
    ⚠️ 每轮枚举对**已有活跃线程**的目标，拿到的句柄必须立刻 CloseHandle：
    那是同一 collection 的第二个句柄，留着就违反单读者原则（互抢帧）。
    """
    k32 = ctypes.windll.kernel32              # type: ignore[attr-defined]
    w32.ensure_argtypes()                     # 幂等；CloseHandle 签名
    threads: dict[str, threading.Thread] = {}
    while not _stop.is_set():
        try:
            targets = _open_targets()
        except Exception as exc:              # noqa: BLE001
            log.debug("dongle open failed: %s", exc)
            targets = []
        for h, name in targets:
            t = threads.get(name)
            if t is not None and t.is_alive():
                k32.CloseHandle(h)            # 已有人听，多余的句柄立刻还回去
                continue
            log.info("2.4G 监听开始: %s", name)
            _touch(name)                      # 登记目标（poll24 按 _stats 决定扫谁）
            t = threading.Thread(target=_watch_one, args=(h, name),
                                 name=f"batbar-24g-{name}", daemon=True)
            threads[name] = t
            t.start()
        for dead in [n for n, t in threads.items() if not t.is_alive()]:
            threads.pop(dead, None)           # 已拔出的设备，字典项清掉
        _stop.wait(10.0)                      # 热插拔检测周期


def start() -> None:
    """启动监听线程（幂等；非 Windows 直接返回）。"""
    global _thread
    if os.name != "nt":
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_watch, name="batbar-24g", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def battery(name: str) -> int | None:
    """本会话从 2.4G 通道收最后一次收到的电量（None = 还没收到过）。"""
    with _lock:
        return _stats.get(name, {}).get("battery")


def seen_path(name: str) -> str | None:
    """该目标的 0xFF02 接口设备路径（_open_targets 枚举到才记）。

    给 poll24 开只写句柄用；设备拔出后路径会残留，但 WriteFile 会失败，
    轮询器静默跳过即可 —— 不用主动清理。
    """
    with _lock:
        return _paths.get(name)


def seen_paths() -> list[str]:
    """当前已知路径的所有目标名（poll24 的轮询清单）。"""
    with _lock:
        return list(_paths)


def battery_age(name: str) -> float | None:
    """最后一次收到电量帧距今多少秒（None = 没收到过）。"""
    with _lock:
        ts = _stats.get(name, {}).get("battery_ts")
    return None if ts is None else max(0.0, time.time() - ts)


def frame_age(name: str) -> float | None:
    """最后一次收到**任何**帧距今多少秒（None = 一次都没收到）。"""
    with _lock:
        ts = _stats.get(name, {}).get("frame_ts")
    return None if ts is None else max(0.0, time.time() - ts)


def last_frame_ts(name: str) -> float | None:
    """最后一次收到任何帧的**时刻**（poll24 扫描判据要的是「这段时间里
    时间戳有没有前进」，用 age 换算窗口在串行扫描下会互相污染）。"""
    with _lock:
        return _stats.get(name, {}).get("frame_ts")


def is_sleeping(name: str) -> bool:
    """键盘是否处于**休眠**（最后一条权威帧是 `type=02 A=00`）。

    注意这**不等于离线**：休眠只是省电静默，键盘仍有电、一碰就醒。而
    「关机/拔电池」在通道上同样表现为静默，两者从帧流上无法区分 —— 所以
    这里只用于显示层标注，不参与离线判定。
    """
    with _lock:
        return bool(_stats.get(name, {}).get("sleeping", False))


def sleep_age(name: str) -> float | None:
    """进入休眠多久了（秒）；没收到过休眠帧返回 None。"""
    with _lock:
        st = _stats.get(name, {})
        ts = st.get("sleep_ts") if st.get("sleeping") else None
    return None if ts is None else max(0.0, time.time() - ts)


def is_offline(name: str) -> bool:
    """是否判定为「不在」（休眠或关机，两者同权 —— 2026-10-12 产品决策）。

    判据（扫描制，无观察窗口）：
      1) 收到过休眠帧（type=02 A=0）→ 立即不在 —— 帧本身就是键盘亲口
         说的「我睡了」，比任何推断都可靠；
      2) 最近一次**扫描**（启动 / 每 5 分钟 / 点「立即刷新」）没有应答 → 不在；
      3) 还没扫过 → 先按不在（启动后 ~2s 内扫完就出结论；旧设计的
         「先显示 90s」已按用户要求删除）。
    扫描之外键盘自己推来的帧（敲键唤醒）也算活证据：`frame_ts` 晚于最近
    一次扫描时刻 → 在线，不等下一轮扫描（「任何时候来一帧立即复活」）。
    """
    from . import poll24
    with _lock:
        st = _stats.get(name)
    if st and st.get("sleeping"):
        return True
    scanned_ts = poll24.scanned_at(name)
    if scanned_ts is None:
        return True                          # 还没扫过 → 先按不在
    if st and st.get("frame_ts") and st["frame_ts"] > scanned_ts:
        return False                         # 扫描后又推来帧 = 被敲醒，立即复活
    return not poll24.online(name)
