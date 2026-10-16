#!/usr/bin/env python3
"""keybeat —— 键盘节拍助手：让我**严格按时间表**敲键，并把 HID 帧对齐到同一条时间轴。

─── 为什么需要它 ──────────────────────────────────────────────
破译 2.4G 接收器协议时，真正缺的不是「有没有帧」，而是**帧与动作的因果**：
type=05 的电量帧到底是不是敲键催出来的？ type=02 的背光帧在多长的静默后
才上报？靠人肉看日志对时间戳，误差动辄几十秒，结论就不可靠。

本脚本把两件事合成一条时间轴：

   ① 到点时**声音 + 任务栏闪烁 + 控制台标题**三路提醒，告诉你该干什么
      （开始敲 / 停手 / 观察多久），不需要你盯着屏幕；
   ② 同时作为**唯一的 HID 读者**收帧，每一帧都打上相对 t0 的毫秒级偏移。

跑完直接输出「指令 → 帧」对照表，并把报告写到磁盘。

⚠️ 前置条件（很重要，违反会得到一份没有数据的报告）
   **必须先退出 batbar 托盘程序**。HID 每个 collection 只有一条输入
   队列，托盘里的 dongle 监听线程和本脚本会**互相抢帧**（实测两个句柄并存
   时两边都收不全）。单读者才能得到完整数据。

▶ 用法（在你的终端里跑，别丢后台 —— 你要听声音、看标题）
    python tools/keybeat.py                       # 默认 wake 方案，约 95 秒
    python tools/keybeat.py --plan sleep          # 验证「真关机/休眠零流量」
    python tools/keybeat.py --every 10 --rounds 6 # 每 10 秒敲一次，敲 6 轮
    python tools/keybeat.py --at 0 15 40 75       # 自定义敲键时刻（秒）
    python tools/keybeat.py --idle 60             # 驱动休眠改了就同步这里
    python tools/keybeat.py --no-sound            # 静音（只用闪烁提醒）

    听到【长响】= 按提示敲 N 下；【两短响】= 立刻停手离开键盘。
    Ctrl+C 可提前结束，已抓到的帧照样出报告。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from ctypes import POINTER, byref, wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ── 协议常数（与 src/batbar/dongle.py 一致，本机 2026-10-12 实测） ──
TARGETS: dict[tuple[int, int], str] = {(0x3554, 0xFA09): "键盘 LEOBOG Hi98C Pro"}
FRAME_LEN = 20
_MAGIC = bytes((0x13, 0x0A, 0x01, 0x00, 0x04))
T_BACKLIGHT, T_BATTERY, T_HANDSHAKE = 0x02, 0x05, 0x07

_lock = threading.Lock()
_frames: list[tuple[float, str, str]] = []  # (自 t0 的秒数, kind, 可读值)
_t0 = time.perf_counter()
_handle = None
_stop = threading.Event()
_link_ok = False  # 是否真的拿到了 HID 句柄
_link_err = ""
_plan: list["Ev"] = []


def now_rel() -> float:
    return time.perf_counter() - _t0


def stamp() -> str:
    """绝对时刻 HH:MM:SS.mmm（毫秒级，这是全脚本时间精度的基准）。"""
    d = datetime.now()
    return d.strftime("%H:%M:%S.") + f"{d.microsecond // 1000:03d}"


# ──────────────────────────  HID 只读监听  ──────────────────────────


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _IFD(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_ulonglong),
    ]


class _DETAIL(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 1024)]


class _CAPS(ctypes.Structure):
    """HIDP_CAPS —— 字段顺序照 MSDN，错一位就读到垃圾长度。"""

    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


def _open_target():
    """找到已知接收器的 0xFF02 输入接口，只读打开。返回 (handle, 友好名)。"""
    setupapi = ctypes.windll.setupapi  # type: ignore[attr-defined]
    hid = ctypes.windll.hid  # type: ignore[attr-defined]
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    guid = _GUID()
    hid.HidD_GetHidGuid.argtypes = [POINTER(_GUID)]
    hid.HidD_GetHidGuid(byref(guid))
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiGetClassDevsW.argtypes = [POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    hdev = setupapi.SetupDiGetClassDevsW(byref(guid), None, None, 0x02 | 0x10)
    if not hdev or hdev == ctypes.c_void_p(-1).value:
        return None, None

    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        POINTER(_GUID),
        wintypes.DWORD,
        POINTER(_IFD),
    ]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE,
        POINTER(_IFD),
        POINTER(_DETAIL),
        wintypes.DWORD,
        POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, POINTER(ctypes.c_void_p)]
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, POINTER(_CAPS)]
    hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]

    found = (None, None)
    i = 0
    while True:
        ifd = _IFD()
        ifd.cbSize = ctypes.sizeof(_IFD)  # 不设 → 第一条就枚举失败
        if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, byref(guid), i, byref(ifd)):
            break
        i += 1
        det = _DETAIL()
        det.cbSize = 8  # x64 对齐后必须是 8
        size = wintypes.DWORD(ctypes.sizeof(_DETAIL))
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(
            hdev, byref(ifd), byref(det), size, byref(size), None
        ):
            continue
        up = det.DevicePath.upper()
        hit = None
        for (vid, pid), name in TARGETS.items():
            if f"VID_{vid:04X}" in up and f"PID_{pid:04X}" in up:
                hit = name
                break
        if not hit:
            continue
        # GENERIC_READ only —— 不申请写权限，本脚本不往设备写一个字节
        h = k32.CreateFileW(det.DevicePath, 0x80000000, 0x01 | 0x02, None, 3, 0, 0)
        if not h or h == ctypes.c_void_p(-1).value:
            continue
        pp = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(h, byref(pp)):
            k32.CloseHandle(h)
            continue
        caps = _CAPS()
        hid.HidP_GetCaps(pp, byref(caps))
        hid.HidD_FreePreparsedData(pp)
        if caps.UsagePage == 0xFF02 and caps.InputReportByteLength >= FRAME_LEN:
            found = (h, hit)
            break
        k32.CloseHandle(h)
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return found


def parse_frame(data: bytes) -> tuple[str, str] | None:
    """解析一帧 -> (kind, 可读值)。帧头不符或 sum8 校验失败返回 None。"""
    if len(data) < FRAME_LEN or bytes(data[:5]) != _MAGIC:
        return None
    if (sum(data[:8]) & 0xFF) != data[FRAME_LEN - 1]:  # 校验位在最后一字节
        return None
    t, a = data[5], data[6]
    if t == T_BATTERY:
        return ("battery", f"电量 {a}%") if 0 <= a <= 100 else None
    if t == T_BACKLIGHT:
        return ("backlight", "背光 亮" if a else "背光 灭")
    if t == T_HANDSHAKE:
        return ("handshake", f"握手 A={a}")
    return ("other", f"type={t:#04x} A={a}")


def push(rel: float, kind: str, text: str) -> None:
    with _lock:
        _frames.append((rel, kind, text))
    print(f"   |-[+{rel:7.2f}s {stamp()}] {kind:<10} {text}", flush=True)


def _reader() -> None:
    global _handle, _link_ok, _link_err
    if sys.platform != "win32":
        _link_err = "非 Windows 平台"
        return
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    try:
        _handle, name = _open_target()
    except Exception as exc:  # noqa: BLE001
        _handle, name, _link_err = None, None, str(exc)
    if not _handle:
        _link_err = _link_err or "没找到接收器（或被其他进程独占）"
        return
    _link_ok = True
    print(f"[link] 已接管 HID 通道: {name} —— 全程单读者", flush=True)
    try:
        buf = (ctypes.c_ubyte * 512)()
        n = wintypes.DWORD(0)
        while not _stop.is_set():
            ok = k32.ReadFile(_handle, buf, 512, byref(n), None)
            if not ok:
                _link_err = f"ReadFile 失败 err={k32.GetLastError()}（拔了？）"
                break
            if n.value:
                got = parse_frame(bytes(buf[: n.value]))
                if got:
                    push(now_rel(), got[0], got[1])
    finally:
        try:
            k32.CloseHandle(_handle)
        except Exception:  # noqa: BLE001
            pass


# ───────────────── 提醒：声音 + 闪烁 + 标题 ─────────────────


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


_sound = True
_idle = 30.0  # 驱动里「休眠」滑块的秒数（判读对照用）


def _beep(freq: int, ms: int) -> None:
    """放子进程里响，避免阻塞时间轴（winsound.Beep 是阻塞的）。"""
    if not _sound or sys.platform != "win32":
        return
    try:
        import winsound

        threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def alert(title: str, *, long_ring: bool = True) -> None:
    """三路提醒。你可能在别的窗口里，光打印看不见，所以：

    · 长响 880Hz        = 现在开始敲
    · 两短响 1568/1245  = 立刻停手（这个区分最重要，手不离开就测不出静默）
    · 任务栏闪烁 + 控制台标题会把当前指令顶出来
    """
    print(f"\n>>> {title}\n", flush=True)
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(f"keybeat: {title}")
            hwnd = ctypes.windll.user32.GetConsoleWindow()
            if hwnd:
                f = _FLASHWINFO()
                f.cbSize = ctypes.sizeof(f)
                f.hwnd = hwnd
                f.dwFlags = 0x00000003  # FLASHW_CAPTION | FLASHW_TRAY
                f.uCount = 5
                f.dwTimeout = 0
                ctypes.windll.user32.FlashWindowEx(byref(f))
        except Exception:  # noqa: BLE001
            pass
    if long_ring:
        _beep(880, 320)
    else:
        _beep(1568, 120)
        time.sleep(0.16)
        _beep(1245, 160)


# ──────────────────────────  时间轴方案  ──────────────────────────


@dataclass
class Ev:
    t: float  # 相对启动的秒
    kind: str  # press | stop | note
    text: str


PLANS: dict[str, list[Ev]] = {
    # 验证：type=05 电量帧是否被「活动」催出 + 停手后多久休眠。
    # 时长按驱动里「休眠=30 秒」设计（2026-10-12 用户改的，原来是约 3 分钟）。
    "wake": [
        Ev(0, "press", "敲 5 下（唤醒键盘、灯点亮）"),
        Ev(20, "press", "再敲 5 下 —— 看电量值是否刷新"),
        Ev(35, "stop", "停手！安静等它休眠（驱动设了 30 秒）"),
        Ev(95, "note", "观察结束"),
    ],
    # 验证：真关机/深休眠后通道是否彻底零流量
    "sleep": [
        Ev(0, "press", "敲 3 下（确认键盘活着）"),
        Ev(5, "stop", "停手！全程不动，观察 240 秒"),
        Ev(245, "note", "观察结束"),
    ],
    # 快速版，适合先看一眼能不能抓到
    "quick": [
        Ev(0, "press", "敲 5 下"),
        Ev(15, "press", "再敲 5 下"),
        Ev(25, "stop", "停手！观察 50 秒"),
        Ev(75, "note", "结束"),
    ],
}


def build_plan(args) -> list[Ev]:
    if getattr(args, "at", None):
        last = float(args.at[-1])
        return [Ev(float(t), "press", f"敲 {args.taps} 下") for t in args.at] + [
            Ev(last + args.tail, "note", "观察结束")
        ]
    if getattr(args, "every", None) is not None:
        rounds = args.rounds or 5
        evs = [Ev(i * args.every, "press", f"敲 {args.taps} 下") for i in range(rounds)]
        evs.append(Ev(rounds * args.every, "stop", "停手！观察收尾"))
        evs.append(Ev(rounds * args.every + args.tail, "note", "观察结束"))
        return evs
    return PLANS[args.plan]


# ──────────────────────────  报告  ──────────────────────────

_KIND_CN = {"battery": "电量", "backlight": "背光", "handshake": "握手", "other": "其他"}


def render_report(plan: list[Ev], desc: str, idle_sec: float = 30.0) -> str:
    frames = sorted(_frames)
    total = max([e.t for e in plan], default=0.0)
    lines = [
        f"# keybeat 报告 · {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        f"方案：{desc}｜观察时长：{total:.0f} 秒｜共收到 **{len(frames)}** 帧",
        "",
        "## 一、指令 → 其后 10 秒内收到的帧",
        "",
        "| 相对时刻 | 指令 | 之后 10s 内来的帧 |",
        "|---|---|---|",
    ]
    for e in plan:
        if e.kind == "note":
            continue
        win = [f"{r:+.1f}s {v}" for r, _k, v in frames if e.t <= r <= e.t + 10]
        tail = "；".join(win) if win else "（无）"
        lines.append(f"| +{e.t:.0f}s | {e.text} | {tail} |")

    lines += [
        "",
        "## 二、原始帧流",
        "",
        "| 相对时刻 | 绝对时刻 | 距上一帧 | 类型 | 值 |",
        "|---|---|---|---|---|",
    ]
    prev = None
    for rel, kind, text in frames:
        gap = "—" if prev is None else f"{rel - prev:.1f}s"
        prev = rel
        lines.append(f"| +{rel:.2f}s | {stamp_rel(rel)} | {gap} | {_KIND_CN.get(kind, kind)} | {text} |")

    # 自动判读：把「敲键 → 电量帧」的因果直接算出来，省得人肉对
    press_ts = [e.t for e in plan if e.kind == "press"]
    bat = [(r, v) for r, k, v in frames if k == "battery"]
    lines += ["", "## 三、自动判读", ""]
    if not frames:
        lines.append(
            "- **全程零帧。** 可能：① 托盘程序没退（抢帧）；② 接收器不在目标接口上；③ 键盘真的关机了。"
        )
    else:
        after = [b for b in bat if any(0 <= b[0] - p <= 10 for p in press_ts)]
        lines.append(f"- 电量帧共 {len(bat)} 条，其中 **{len(after)} 条**紧跟在敲键指令后 10 秒内。")
        if bat and len(after) == len(bat):
            lines.append(
                "  - ⇒ **电量帧只在键盘活动时出现**；静默期拿不到新读数，"
                "这是事件驱动通道的正常表现（不是 bug）。"
            )
        elif bat and not after:
            lines.append("  - ⇒ 电量帧与敲键**无关**，大概率是键盘自发周期上报。")
        stops = [e.t for e in plan if e.kind == "stop"]
        if stops:
            idle = stops[0]
            silent = [r for r, _k, _v in frames if r > idle + 15]
            lines.append(
                f"- 停手 15 秒后又来 {len(silent)} 帧"
                + (
                    f"，最后一次在 +{max(silent):.0f}s。"
                    if silent
                    else "，**完全静默** —— 支持「关机/深睡 ⇒ 零流量」判据。"
                )
            )
            # 对照驱动里的「休眠」滑块：停手后第一条背光灭帧应该 ≈ 设定值
            bl_off = [r for r, k, v in frames if k == "backlight" and r > idle and "灭" in v]
            if bl_off:
                d = bl_off[0] - idle
                lines.append(
                    f"- 停手 → 背光灭帧延迟 **{d:.1f}s**"
                    f"（驱动休眠设定 {idle_sec:g}s，偏差 {d - idle_sec:+.1f}s）。"
                )
            elif any(k == "backlight" for _r, k, _v in frames):
                lines.append(
                    f"- 停手后 {idle_sec:g}s 内**没有**背光灭帧 ——"
                    "要么休眠上报不走这条通道，要么延迟明显大于设定值，值得注意。"
                )
    return "\n".join(lines) + "\n"


def stamp_rel(rel: float) -> str:
    """把相对偏移换算成绝对 HH:MM:SS（报告里便于与实际操作时刻对照）。"""
    base = datetime.fromtimestamp(time.time() - now_rel() + rel)
    return base.strftime("%H:%M:%S")


def main() -> int:
    global _sound, _plan, _idle
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 中文窗必需

    ap = argparse.ArgumentParser(description="键盘节拍助手：按时间表敲键，并把 HID 帧对齐到同一时间轴")
    ap.add_argument("--plan", choices=sorted(PLANS), default="wake")
    ap.add_argument("--at", type=float, nargs="+", metavar="SEC", help="自定义敲键时刻（秒，可多个）")
    ap.add_argument("--every", type=float, metavar="SEC", help="每隔几秒敲一次")
    ap.add_argument("--rounds", type=int, default=5, help="配合 --every，敲几轮")
    ap.add_argument("--taps", type=int, default=5, help="每次敲几下（默认 5）")
    ap.add_argument("--tail", type=float, default=60, help="最后一次敲键后再观察多少秒（默认 60）")
    ap.add_argument("--out", default="_tmp", help="报告输出目录")
    ap.add_argument("--idle", type=float, default=30, help="驱动「休眠」滑块的秒数，判读对照用（默认 30）")
    ap.add_argument("--no-sound", action="store_true", help="静音，只闪烁提醒")
    ap.add_argument("--selftest", action="store_true", help="自检：不接 HID、加速时间轴、喂合成帧")
    args = ap.parse_args()
    _sound = not args.no_sound
    _idle = args.idle
    if args.at and args.tail < 30:
        args.tail = 30
    speed = 0.02 if args.selftest else 1.0

    _plan = build_plan(args)
    desc = (
        "自定义时刻 " + ", ".join(f"{t:g}s" for t in args.at)
        if args.at
        else f"每 {args.every:g}s × {args.rounds} 轮"
        if args.every is not None
        else args.plan
    )

    print("=" * 66)
    print(" keybeat —— 键盘节拍助手")
    print(" 【长响】= 开始敲键    【两短响】= 立刻停手，把手拿开")
    if not args.selftest:
        print(" !! 必须已退出 batbar 托盘程序，否则两个句柄抢帧、收不全数据")
    print("=" * 66)

    if args.selftest:
        threading.Thread(
            target=lambda: [
                push(0.4, "battery", "电量 93%"),
                push(1.6, "backlight", "背光 亮"),
                push(20.3, "battery", "电量 93%"),
            ],
            daemon=True,
        ).start()
    else:
        threading.Thread(target=_reader, daemon=True).start()
        time.sleep(1.0)
        if not _link_ok:
            print(f"\n[x] 没拿到 HID 通道：{_link_err}")
            print("    多半是托盘程序还在跑（抢帧），或接收器没插。")
            print("    时间轴照常走，但报告里不会有任何帧。Ctrl+C 可中断。\n")

    alert(f"开始 · {desc}", long_ring=True)
    for e in _plan:
        wait = max(0.0, (e.t - now_rel()) * speed)
        if wait:
            end = time.perf_counter() + wait
            while True:
                left = end - time.perf_counter()
                if left <= 0:
                    break
                if left <= 3:  # 最后 3 秒倒计时，好把手放上键盘
                    print(f"\r  ... {left:4.1f}s 后：{e.text[:24]}", end="", flush=True)
                    time.sleep(0.1)
                else:
                    time.sleep(min(0.25, max(0.05, left - 3)))
            print("\r" + " " * 60 + "\r", end="", flush=True)
        if e.kind == "press":
            alert(f"[{e.t:>6.0f}s] {e.text}", long_ring=True)
        elif e.kind == "stop":
            alert(f"[{e.t:>6.0f}s] {e.text}", long_ring=False)
        else:
            alert(f"[{e.t:>6.0f}s] {e.text}", long_ring=True)
    _stop.set()
    time.sleep(0.4)

    report = render_report(_plan, desc, _idle)
    print()
    print(report)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"keybeat-{datetime.now():%Y%m%d-%H%M%S}.md"
    p.write_text(report, encoding="utf-8")
    print(f"报告已写入：{p.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _stop.set()
        print("\n\n[中断] 提前结束，下面是已抓到的帧：\n")
        print(render_report(_plan, "中断收集", _idle))
