"""Windows 设备/任务栏事件监听：把系统广播转成"该刷新了"的回调。

── 为什么需要它（2026-10-13 需求）──
当前 agent 全靠**定时轮询**感知设备变化：鼠标 ~9s 一轮、蓝牙 ~30s、2.4G 60s。
拔掉接收器 / 设备开关机后，最短也要等一个轮询周期才更新。而 Windows 其实会
**主动广播**设备变化（`WM_DEVICECHANGE`）—— 收到就能立即刷新，时延从秒级
降到毫秒级，还顺带省了无谓的轮询。

同时 `WM_TASKBARCREATED` 能让托盘图标在 **explorer 崩溃重启后自动重建**
（现在没监听，explorer 一崩图标就永久消失，只能手动重启 agent）。

── 实测取证（2026-10-13 18:17，`probes/probe_device_events2.py`）──
一次「拔接收器 → 等 3s → 插回」的实测序列（4 种窗口样式全部收到，一条不差）：

    18:17:49  DBT_DEVNODES_CHANGED        ← 拔（设备树变化）
    18:17:50  DBT_DEVICEREMOVECOMPLETE    ← 移除确认
    18:17:55  DBT_DEVICEARRIVAL           ← 插回
    18:17:55  DBT_DEVNODES_CHANGED × 12   ← ★ 连发十几条！

两条关键结论：
  1. **普通隐藏 POPUP 窗口就能收到广播**（不需要可见、不需要特殊样式、
     不需要 RegisterDeviceNotification 也能收到 NODES_CHANGED）；
  2. **一次插拔会产生 10+ 条 NODES_CHANGED** ⇒ **必须去抖**，否则一次插拔
     触发十几次全量刷新，白烧 CPU（正是本项目要解决的问题）。

── 分层（守项目铁律：纯逻辑与 I/O 分离）──
  * `EventDebouncer` —— **纯逻辑**：把密集事件合并成"最后一次之后的静默边界
    触发一次"。可在任意平台离线单测。
  * 窗口创建 / 消息循环 —— Windows-only，由 `agent._start_theme_watch` 所在的
    那个隐藏窗口承载（复用，不新建窗口）。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("batbar.event")

# ── Windows 消息常量 ──
WM_DEVICECHANGE = 0x0219
DBT_DEVICEARRIVAL = 0x8000          # 设备接入
DBT_DEVICEREMOVECOMPLETE = 0x8004   # 设备移除
DBT_DEVNODES_CHANGED = 0x0007       # 设备树变化（最频繁，需去抖）

# 去抖窗口（秒）。实测一次插拔的 NODES_CHANGED 连发集中在 ~1s 内（12 条挤在
# 同一秒），取 600ms 足够把一波合并成一次刷新，又不会让用户感到延迟。
DEBOUNCE_SECONDS = 0.6


class EventDebouncer:
    """把密集的同类事件合并成一次触发（纯逻辑，可脱机单测）。

    语义：每次 `bump()` 记录一个"最近事件时刻"并置脏；`due(now)` 在**距最近
    事件已超过 quiet 秒**时返回 True 并清脏。这样一波 12 条 NODES_CHANGED 只会
    在尾声触发一次，而不是 12 次。

    为什么用"静默边界"而不是"首次立即触发 + 冷却"：设备插拔的实际有效信息在
    最后一帧（插回那一刻设备才真正就绪），静默边界天然对齐"这一波结束了"。
    """

    def __init__(self, quiet: float = DEBOUNCE_SECONDS) -> None:
        """
        Args:
            quiet: 静默判定窗口（秒）。距最近事件超过它才算"这一波结束"。
        """
        self._quiet = quiet
        self._last = 0.0
        self._dirty = False

    def bump(self, now: float | None = None) -> None:
        """记录一次事件到达。"""
        self._last = time.monotonic() if now is None else now
        self._dirty = True

    def due(self, now: float | None = None) -> bool:
        """距最近事件已静默超过 quiet 秒且有待触发？是则消费脏位并返回 True。"""
        if not self._dirty:
            return False
        now = time.monotonic() if now is None else now
        # 用 `>= quiet - 1e-9` 而非 `>= quiet`：浮点减法会算成 0.5999999…
        # （如 100.6 - 100.0），严格比较会让"正好到点"判不中。
        if now - self._last >= self._quiet - 1e-9:
            self._dirty = False
            return True
        return False

    @property
    def pending(self) -> bool:
        """是否还有未触发的事件（供轮询线程判断要不要继续查）。"""
        return self._dirty

    def next_deadline(self) -> float | None:
        """下一次可能 due 的时刻（monotonic）；无待触发返回 None。"""
        if not self._dirty:
            return None
        return self._last + self._quiet


def classify(wparam: int) -> str | None:
    """WM_DEVICECHANGE 的 wparam → 语义标签（纯逻辑，可单测）。

    Returns:
        "arrival" / "remove" / "nodes" / None（无关的子类型，忽略）。
    """
    if wparam == DBT_DEVICEARRIVAL:
        return "arrival"
    if wparam == DBT_DEVICEREMOVECOMPLETE:
        return "remove"
    if wparam == DBT_DEVNODES_CHANGED:
        return "nodes"
    return None


# ═══════════════════════════ RAWINPUT 鼠标唤醒（2026-10-13）═══════════════════════════
#
# ── 要解决的问题 ──
# 罗技条目在/离线靠 `_probe_mouse`（~9s 一轮）与「两次不应答 = 不在」判据。
# 鼠标关机后条目会隐藏，但**重新开机后要等下一轮轮询才复活**（退避还能到 30s）。
# 用户把鼠标拿起来晃两下、托盘要等好几秒才恢复 —— 感知上就是「卡」。
#
# ── 为什么 RAWINPUT 能补这一刀 ──
# `WM_INPUT` + `RIDEV_INPUTSINK` 让**隐藏、无焦点**的窗口也能收到全局鼠标输入
# （2026-10-13 实测：`probes/probe_rawinput.py` 隐藏窗口 15s 收到 322 条 WM_INPUT，
# 全部 type=mouse；`probes/probe_rawinput_size.py` 确认尺寸语义）。
# 「鼠标此刻有活动」= 「它醒着」——收到就**立即触发一次探测**，不必等轮询。
#
# ⚠️ 三条边界（很重要，别把 RAWINPUT 当罗技专属信号）：
#   1. **拿不到是哪只鼠标**：RAWINPUT 是全局输入，触摸板/别的鼠标/远程桌面
#      的活动都会进来。所以它只能当「**催化剂**」（催一次真探测），**绝不能**
#      当「罗技鼠标在线」的判据 —— 在线与否永远由 HID++ 应答决定。
#   2. **不是罗技私有信息**：罗技的睡眠/唤醒在 HID++ 层有 frame，RAWINPUT 没有。
#   3. **频率极高**（实测 8s 262 条）⇒ **必须去抖**，否则一次拖动就催几十次探测。
#
# ── 分层（守项目铁律：纯逻辑与 I/O 分离）──
#   * 本节的 `decode_rawinput` / `is_mouse_input` —— **纯逻辑**：喂原始字节返回
#     结构化结果，可在任意平台离线单测（本机 ctypes 那层在 agent）。
#   * `RegisterRawInputDevices` / `GetRawInputData` —— Windows-only，在 agent
#     的隐藏消息窗口里做。

WM_INPUT = 0x00FF              # 原始输入到达（RIDEV_INPUTSINK 下后台也收）

RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RIM_TYPEHID = 2

# ⚠️ `usFlags` 里**只有移动类标志**；按键/滚轮位在 `ulButtons`（union 低 16 位）。
# 这条注释是 2026-10-13 实测踩坑后写的：一开始误以为按键位在 usFlags，
# 结果「纯点击（光标不动）」会被判成无活动。见 `decode_rawinput`。
MOUSE_MOVE_RELATIVE = 0x00
MOUSE_MOVE_ABSOLUTE = 0x01
MOUSE_ATTRIBUTES_CHANGED = 0x04

# ── ulButtons（union，RAWMOUSE 偏移 4）里的位 ──
MOUSE_BUTTON_1_DOWN = 0x0001
MOUSE_BUTTON_1_UP = 0x0002
MOUSE_BUTTON_2_DOWN = 0x0004
MOUSE_BUTTON_2_UP = 0x0008
MOUSE_BUTTON_3_DOWN = 0x0010
MOUSE_BUTTON_3_UP = 0x0020
MOUSE_BUTTON_4_DOWN = 0x0040
MOUSE_BUTTON_4_UP = 0x0080
MOUSE_BUTTON_5_DOWN = 0x0100
MOUSE_BUTTON_5_UP = 0x0200
MOUSE_BUTTON_MASK = 0x07FF          # 上面所有按键位的并集
MOUSE_WHEEL = 0x0400
MOUSE_HWHEEL = 0x0800

# ── 结构体尺寸（x64，2026-10-13 实测）──
# RAWINPUTHEADER = dwType(4) + dwSize(4) + hDevice(8) + wParam(8) = 24
# RAWMOUSE       = usFlags(2)+pad(2) + ulButtons(4) + ulRawButtons(4)
#                  + lLastX(4) + lLastY(4) + ulExtraInformation(4) = 24
# 整条 mouse input = 24 + 24 = 48（probe 实测 pcbSize 上报 48）
RAWINPUTHEADER_SIZE = 24
RAWMOUSE_SIZE = 24

# RAWINPUT 去抖窗口（秒）。鼠标拖动时事件极密（实测 ~33 条/s），取 0.25s：
# 够把一次连续移动合并成一次探测，又不会让用户觉得"晃了半天没反应"。
# 比设备事件的 0.6s 短 —— 设备插拔一波是真的一大串（12 条挤 1s），
# 而鼠标活动是"用户正在操作"的即时信号，要让复活尽量跟手。
RAWINPUT_DEBOUNCE_SECONDS = 0.25


def is_mouse_input(raw: bytes) -> bool:
    """这条 RAWINPUT 是不是鼠标设备（纯逻辑，可单测）。

    Args:
        raw: 至少含 RAWINPUTHEADER（前 24 字节）的原始缓冲。
    Returns:
        True = 鼠标（RIM_TYPEMOUSE）；数据太短或非鼠标返回 False。
    """
    if len(raw) < 8:
        return False
    dw_type = int.from_bytes(raw[0:4], "little")
    return dw_type == RIM_TYPEMOUSE


def decode_rawinput(raw: bytes) -> dict | None:
    """解析一条 RAWINPUT（纯逻辑，可单测）。

    ⚠️ 本函数**只解鼠标**（本项目不关心键盘/HID 原始输入）。非鼠标返回 None。

    真实布局（x64，2026-10-13 实测 sizeof 校验 + 字段回读验证）：
        RAWINPUTHEADER（24B）:
            偏移 0  dwType(4)       偏移 4  dwSize(4)
            偏移 8  hDevice(8)      偏移 16 wParam(8)
        RAWMOUSE（24B，从偏移 24 起）：
            偏移 24 usFlags(2)      偏移 26 保留/对齐(2)
            偏移 28 ulButtons(4)    ← ★ 按键位在这里，**不在 usFlags**
            偏移 32 ulRawButtons(4)
            偏移 36 lLastX(4，有符号)  偏移 40 lLastY(4，有符号)
            偏移 44 ulExtraInformation(4)

    ⚠️ 最容易踩的坑：以为按键在 `usFlags`（错）。`usFlags` 只有移动标志
    （RELATIVE/ABSOLUTE/ATTRIBUTES_CHANGED），按键与滚轮全在
    `ulButtons`（union：低 16 位 usButtonFlags + 高 16 位 usButtonData）。
    本项目只需要「这台鼠标动没动」，但若把按键读错字段，纯点击（不动光标）
    会被漏掉。

    Returns:
        {"kind": "move"|"button"|"wheel"|"other", "dx": int, "dy": int,
         "flags": int(usFlags), "buttons": int(ulButtons)}
        或 None（太短 / 非鼠标）。
    """
    if len(raw) < RAWINPUTHEADER_SIZE + RAWMOUSE_SIZE:
        return None
    if not is_mouse_input(raw):
        return None
    us_flags = int.from_bytes(raw[24:26], "little")
    buttons = int.from_bytes(raw[28:32], "little")
    dx = int.from_bytes(raw[36:40], "little", signed=True)
    dy = int.from_bytes(raw[40:44], "little", signed=True)
    if buttons & (MOUSE_WHEEL | MOUSE_HWHEEL):
        kind = "wheel"
    elif buttons & 0x07FF:                # BUTTON_1..BUTTON_5 的 down/up 位
        kind = "button"
    elif us_flags & (MOUSE_MOVE_ABSOLUTE | MOUSE_MOVE_RELATIVE):
        kind = "move"
    elif dx != 0 or dy != 0:              # 兜底：有位移就算移动
        kind = "move"
    else:
        kind = "other"
    return {"kind": kind, "dx": dx, "dy": dy,
            "flags": us_flags, "buttons": buttons}


def rawinput_activity(raw: bytes) -> bool:
    """这条输入算不算「鼠标有真实活动」（纯逻辑，可单测）。

    用来过滤"空移动"（dx=dy=0 且无按键/滚轮）——RAWINPUT 偶尔会送这种
    心跳式的零位移帧，它们不代表用户真的动了鼠标，不该催探测。
    """
    info = decode_rawinput(raw)
    if info is None:
        return False
    if info["kind"] in ("button", "wheel"):
        return True
    return info["dx"] != 0 or info["dy"] != 0
