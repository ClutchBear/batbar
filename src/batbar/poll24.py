"""2.4G 电量轮询器 —— 周期性发官方 getBattery 查询帧，刷新静默期的旧读数。

── 背景（2026-10-12 实测定案） ──
迈从 K87 的 2.4G 通道是**事件驱动**推送：只在开机重连、插拔充电线、灯效
变化时推一帧电量，平时静默 —— 托盘在两次事件之间显示的是旧值（Hub 显示
100%、托盘停在 98% 就是这么来的）。

官方 Hub 网页会发**查询命令**，设备会回命令应答帧：

    发（output，20B）：13 4a 01 00×16 5e      ← sum8 校验（13+4a+01=0x5E）
    收（input， 20B）：13 4a 01 00 02 <pct> <chg> … <chk>

16:56 监听窗口实抓两笔应答（93%），sum8 含首字节 0x13 验算通过；查询帧
格式从官方 Hub bundle（本地留存的 hub_protocol.js 的 `o=`${l} ${X3e(l)}``）逆向
得来，X3e 就是逐字节求和的校验函数。

── 设计（2026-10-12 21:00 改为**扫描制**，用户拍板） ──
- **扫描 = 在/离线判据**：逐个目标发官方查询帧，等 `SETTLE`(1.5s) 看有没有
  应答 —— 应答帧由 dongle 监听线程解析入库（frame_ts 更新），所以「扫完
  看 frame_ts 是否前进」就是设备是否在线的硬证据。**取消观察窗口**。
- **节奏**：启动扫一遍 → 之后每 `SCAN_INTERVAL`(60s) 一遍 →
  托盘菜单「立即刷新」再扫一遍（`scan_now()`）。想调快慢用
  `BATBAR_SCAN_24G`（秒）。
- **独立只写句柄**（GENERIC_WRITE）：单读者原则只约束输入队列 —— dongle
  的监听线程照常独占读，本模块只写不读，两者互不干扰。打开句柄/写失败
  （设备刚拔出等）静默跳过，下轮再试 —— 热插拔自愈，不需要额外状态机。
- 键盘**休眠时设备不回帧**（实测）：扫描判它不在 → 条目隐藏，符合
  「休眠与关机同权」的产品决策。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes

from . import dongle

log = logging.getLogger("batbar.poll24")

SCAN_INTERVAL = 60.0          # 扫描间隔：60s（BATBAR_SCAN_24G 可覆盖）。
                              # bug 13（2026-10-13）：原 300s 让 2.4G 键盘关机
                              # 后自动消失要等 5 分钟；用户要求「一分钟左右」，
                              # 扫描本身是 HID 帧查询（~1.5s/设备），60s 节奏
                              # 无 CPU/硬件压力。
SETTLE = 1.5                  # 发查询后等应答的窗口（秒）

# 官方 getBattery 无线查询帧（含 sum8 校验尾，共 20B = 0xFF02 的
# OutputReportByteLength，首字节 0x13 兼作 report ID 与协议 magic）。
QUERY_FRAME = bytes(bytearray([0x13, 0x4A, 0x01] + [0x00] * 16)) + bytes([0x5E])

GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x01 | 0x02
OPEN_EXISTING = 3

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_polled: dict[str, float] = {}          # name -> 上次成功发出的时刻（诊断用）
_online: dict[str, bool] = {}           # name -> 最近一次扫描结论
_scanned_at: dict[str, float] = {}      # name -> 最近一次扫描时刻
# 可注入的「此刻该跳过扫描吗」判据（2026-10-13 全屏/游戏感知）。默认 None =
# 从不跳过。agent 启动时 set_skip_when(gate) 注入 —— 这样 poll24 保持自己的
# 模块独立性（不 import agent），又能感知全屏。
_skip_when = None                       # type: ignore[var-annotated]


def set_skip_when(pred) -> None:
    """注入「该跳过扫描吗」判据（无参函数 → bool）。传 None 取消。"""
    global _skip_when
    _skip_when = pred


def _should_skip() -> bool:
    if _skip_when is None:
        return False
    try:
        return bool(_skip_when())
    except Exception:  # noqa: BLE001 - 判据失败按"不跳过"处理
        return False


def scan_interval() -> float:
    """扫描间隔（秒）：BATBAR_SCAN_24G 覆盖，默认 300s。"""
    try:
        raw = os.environ.get("BATBAR_SCAN_24G")
        return float(raw) if raw else SCAN_INTERVAL
    except ValueError:
        return SCAN_INTERVAL


def query_frame() -> bytes:
    """构造查询帧（独立函数方便测试与将来扩展其它 opcode）。"""
    body = bytearray([0x13, 0x4A, 0x01])
    body.extend([0x00] * 16)
    body.append(sum(body) & 0xFF)
    return bytes(body)


def poll_once(name: str) -> bool:
    """对一个接收器发一次查询帧。发出 True / 任何失败 False。

    每次现开现关句柄：轮询频率低（分钟级），不值得常驻句柄，还免了
    设备拔出时的句柄失效清理。
    """
    path = dongle.seen_path(name)
    if not path:
        return False
    k32 = ctypes.windll.kernel32              # type: ignore[attr-defined]
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p]
    k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.CreateFileW(path, GENERIC_WRITE, FILE_SHARE_RW, None,
                        OPEN_EXISTING, 0, 0)
    if not h or h == ctypes.c_void_p(-1).value:
        log.debug("poll24 %s 打开只写句柄失败 err=%s", name,
                  ctypes.get_last_error())
        return False
    try:
        n = wintypes.DWORD(0)
        buf = ctypes.create_string_buffer(QUERY_FRAME, len(QUERY_FRAME))
        ok = k32.WriteFile(h, buf, len(QUERY_FRAME), ctypes.byref(n), None)
        if not ok or n.value != len(QUERY_FRAME):
            log.debug("poll24 %s 写查询帧失败 ok=%s n=%s", name, ok, n.value)
            return False
        with _lock:
            _polled[name] = time.time()
        log.debug("poll24 %s 查询帧已发（等 4a 应答）", name)
        return True
    finally:
        k32.CloseHandle(h)


def polled_age(name: str) -> float | None:
    """上次成功发出查询距今多少秒（None = 从未发出过；诊断用）。"""
    with _lock:
        ts = _polled.get(name)
    return None if ts is None else max(0.0, time.time() - ts)


def scan(names: list[str] | None = None) -> dict[str, bool]:
    """扫描一轮：逐个目标发查询帧、等应答，返回 {名: 是否在线}。

    在线判据 = **本轮扫描窗口内该设备的 frame_ts 有前进**（应答帧或推送帧
    都算，`dongle.last_frame_ts`）。用「时间戳有没有变」而不是「age 多小」，
    是因为串行扫描下前一台的等待窗口会被后一台的旧帧污染 —— 时间戳前进是
    每台各自的硬证据，互不干扰。**没有观察窗口**：扫完立即下结论。

    未传 names 时扫 dongle 当前登记的全部目标。

    Args:
        names: 要扫的目标名；None = `dongle.seen_paths()`。
    Returns:
        {名: 在线}（未登记路径的目标也会给结论：不在线）。
    """
    targets = list(names) if names is not None else list(dongle.seen_paths())
    out: dict[str, bool] = {}
    for name in targets:
        before = dongle.last_frame_ts(name)     # 本轮起点的帧时刻（None = 还没有过帧）
        poll_once(name)                         # 发不出去（句柄故障）也照样等窗口：
        time.sleep(SETTLE)                      # 设备自己推来的帧同样是活证据
        after = dongle.last_frame_ts(name)
        # frame_ts 前进 = 本窗口内确实收到过新帧（应答或推送）= 在线
        out[name] = after is not None and after > (before or 0)
    with _lock:
        _online.update(out)
        now = time.time()
        for n in out:
            _scanned_at[n] = now
    log.info("扫描完成：%s", {k: ("在线" if v else "不在") for k, v in out.items()})
    return out


def scan_now(names: list[str] | None = None) -> dict[str, bool]:
    """同步扫一轮（「立即刷新」用；调用方需自带线程，约 1.5s/设备）。"""
    return scan(names)


def online(name: str) -> bool:
    """最近一次扫描的结论（还没扫过 → False，即先按不在处理）。"""
    with _lock:
        return bool(_online.get(name, False))


def scanned(name: str) -> bool:
    """这台设备有没有被扫描过（决定 is_offline 是「有结论」还是「等结论」）。"""
    with _lock:
        return name in _scanned_at


def scanned_at(name: str) -> float | None:
    """最近一次扫描出结论的时刻（dongle.is_offline 用它比较「扫描之后有没有
    再来帧」——扫描判不在、随后又推来帧 = 键盘被敲醒，立即复活）。"""
    with _lock:
        return _scanned_at.get(name)


def reset() -> None:
    """仅测试用：清空扫描结论。"""
    with _lock:
        _online.clear()
        _scanned_at.clear()
        _polled.clear()


def _loop() -> None:
    """启动扫一遍，之后每 scan_interval() 秒一遍。

    ⚠️ 首轮前等 dongle 的枚举线程把设备路径登记进来（最多 10s）：路径表
    为空时扫了也是空结果，所有设备都拿不到结论，要白等一整个 5 分钟。
    """
    for _ in range(20):
        if dongle.seen_paths():
            break
        _stop.wait(0.5)
    while not _stop.is_set():
        # 路径表由 dongle 的枚举线程维护；有谁就扫谁（迈从 + LEOBOG，
        # 命令不适用时设备直接忽略，零风险）。
        # 全屏/游戏中跳过本轮（2026-10-13）：此刻用户不看托盘，且游戏已吃满
        # 资源，白扫一轮只添噪音。用短睡轮询（而非直接 `wait(interval)`）
        # 是为了退出全屏后能**尽快**恢复扫描，不必再等一整个 60s。
        if _should_skip():
            _stop.wait(5.0)
            continue
        try:
            scan()
        except Exception as exc:              # noqa: BLE001 - 单轮失败下轮再试
            log.debug("poll24 scan: %s", exc)
        _stop.wait(scan_interval())


def start() -> None:
    """启动轮询线程（幂等；非 Windows 直接返回）。"""
    global _thread
    if os.name != "nt":
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="batbar-poll24", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
