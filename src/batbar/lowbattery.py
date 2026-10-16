"""低电量提醒（带去抖 + 迟滞，抄自 MCHOSE-Battery 的产品逻辑并本机加固）。

── 为什么要去抖 ──
2.4G 通道是事件驱动的，链路切换/键盘唤醒的瞬间固件常吐一个「无设备帧」，
电量字节读成 0。若「读到 ≤ 阈值就弹」，用户会在根本没低电时被 0% 吓一跳
（MCHOSE-Battery v3.1.1 的 issue 就是这个）。所以：

  · **连续 3 次**读到 ≤ `threshold` 才触发一次提醒（约覆盖 3 个刷新周期，
    能滤掉孤立的 0 帧毛刺）；
  · 触发后**静默**，直到该设备电量回升到 `threshold + reset_gap`（默认 +5，
    即 ≥20%）才重新布防 —— 迟滞带防止在阈值线上反复弹；
  · **充电中 / 有线** 豁免：插着线谈不上「快没电」，不弹。

── 提醒走 pystray，不走 winotify ──
托盘图标本来就是 pystray.Icon，直接 `icon.notify(title, msg)` 复用它的消息
循环，零新依赖；`notify` 在部分后端要求传入一个 callback，这里给空函数兜底。

纯判定逻辑（`feed()`）与「弹通知」的副作用（`_deliver`）分离，前者可脱机
单测，后者只在 agent 接线里被调用。
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("batbar.lowbat")

DEFAULT_THRESHOLD = 20  # 百分比：≤ 它算「低」（2026-10-12 用户拍板「低于 20 长显」）
DEFAULT_RESET_GAP = 5  # 迟滞：回升到 threshold+gap 才重新布防
DEFAULT_CONFIRM = 3  # 连续几次低读数才触发（防 0 帧毛刺）
HEADPHONE_PREFIX = "耳机"  # 友好名命名规范「类别 品牌+型号」→ 耳机类以此开头


def threshold() -> int:
    """阈值百分比，BATBAR_LOWBAT_THRESHOLD 可覆盖（对齐用户此前偏好的调参方式）。"""
    try:
        raw = os.environ.get("BATBAR_LOWBAT_THRESHOLD")
        return int(raw) if raw else DEFAULT_THRESHOLD
    except ValueError:
        return DEFAULT_THRESHOLD


def toast_enabled() -> bool:
    """系统通知开关。2026-10-12 起默认**关**：用户不看小铃铛通知，改为托盘
    图标红字示警（agent._apply_icon）。想恢复通知设 BATBAR_LOWBAT_TOAST=1。"""
    return os.environ.get("BATBAR_LOWBAT_TOAST", "") == "1"


def pick_display(
    snap: dict[str, int | None],
    wired_bases: set[str] | frozenset[str] = frozenset(),
    charging_names: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, int] | None:
    """挑托盘图标要显示的那台设备：**所有设备里电量最低的一台**（2026-10-12 用户拍板）。

    排除项（纯函数，脱机可测）：
      * `None`（无读数）与 `0`（2.4G 链路毛刺常吐假 0，见本模块 docstring）；
      * 有线 / 充电中的设备 —— 插着电的设备不是「需要关注」的对象，
        让它抢图标会变成假警报。
    多台同电取先出现的那台（dict 顺序稳定）。key 一律是**剥后缀基名**，
    显示名由调用方负责还原。
    """
    best: tuple[str, int] | None = None
    for name, pct in snap.items():
        if pct is None or pct <= 0:
            continue
        if name in wired_bases or name in charging_names:
            continue
        if best is None or pct < best[1]:
            best = (name, pct)
    return best


def pick_preferred(
    snap: dict[str, int | None],
    wired_bases: set[str] | frozenset[str] = frozenset(),
    charging_names: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, int] | None:
    """无低电设备时挑「优先关注」的一台：**耳机优先**（2026-10-12 用户拍板）。

    耳机是「不盯着就容易突然没电」的设备（鼠标/键盘没电了随手换线就行），
    所以自动规则在没有低电设备时优先显示**电量最低的那只耳机**；一台耳机
    都没有才返回 None（由调用方回落跟踪源）。排除项与 `pick_display` 一致。
    """
    hp = {n: p for n, p in snap.items() if n.startswith(HEADPHONE_PREFIX)}
    return pick_display(hp, wired_bases, charging_names)


def pick_low(
    snap: dict[str, int | None],
    thr: int,
    wired_bases: set[str] | frozenset[str] = frozenset(),
    charging_names: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, int] | None:
    """从快照里挑「最该警告」的低电设备，返回 (剥后缀基名, pct)。

    判据：0 < pct ≤ thr 且非有线非充电（纯函数，脱机可测）。0% 排除——2.4G
    链路毛刺会把电量读成 0（本模块 docstring 的历史坑）。多台低电取**最低**
    那台。显示名 → 基名由调用方处理；这里拿到的 key 已是基名。
    """
    hit = pick_display(snap, wired_bases, charging_names)
    if hit is None or hit[1] > thr:
        return None
    return hit


class LowBatteryWatcher:
    """跨设备各记一份「连续低读数 / 是否已布防」的小状态机。

    feed() 每次拿到一次读数就调用一次，返回 True 表示「此刻应当弹一条通知」。
    """

    def __init__(
        self,
        icon_getter=None,
        confirm: int = DEFAULT_CONFIRM,
        reset_gap: int = DEFAULT_RESET_GAP,
        wired_check=None,
        gate=None,
    ):
        # icon_getter: 无参函数 → 返回当前 pystray.Icon 或 None（延迟解耦，
        # 单测不需要托盘）。
        # wired_check: name -> bool；**只在即将弹的前一刻**调用一次实时复核
        # （它可能要起 USB 枚举，每次 feed 都调太贵）。feed() 里传的 wired
        # 是 TTL 缓存值，可能滞后一个周期 —— 触发前再 force 查一次，宁可
        # 多花一次枚举也绝不在插线充电时弹假警报。
        # gate: (name, pct) -> bool；**允许弹？** True 才真弹（2026-10-13 全屏
        # 感知）。延迟到投递那一刻才问 —— 全屏状态秒级变化，若在 feed 入口判，
        # 状态机内部的 confirm 计数会跟着断。返回 False 时**不弹但也不补弹**，
        # 由调用方决定是否记账（见 agent._gate_notify）。
        self._icon_getter = icon_getter
        self._wired_check = wired_check
        self._gate = gate
        self._confirm = max(1, confirm)
        self._reset_gap = reset_gap
        self._thr = threshold()
        self._misses: dict[str, int] = {}  # 设备名 -> 连续低读数计数
        self._armed: dict[str, bool] = {}  # 设备名 -> 是否仍可触发（布防）
        self._last_notify_ts: dict[str, float] = {}

    def feed(self, name: str, pct: int | None, charging: bool = False, wired: bool = False) -> bool:
        """喂一次读数。返回 True = 应弹通知（本方法已顺手弹）。"""
        thr = self._thr
        # 读不到 / 充电 / 有线：撤销计数并（在明显不低时）重新布防，绝不弹。
        if pct is None or charging or wired:
            self._misses[name] = 0
            if charging or wired:
                # 插着线/充电中是「安全态」，重新布防，等下次拔线真低电再提醒
                self._armed[name] = True
            return False

        if pct <= thr:
            self._misses[name] = self._misses.get(name, 0) + 1
            # 只有仍处于布防态、且连续低读数达标，才触发一次并解除布防
            if self._armed.get(name, True) and self._misses[name] >= self._confirm:
                # 弹之前实时复核一次有线（feed 里的 wired 是 TTL 缓存值，
                # 可能滞后；插线充电被误弹 = 假警报，代价最高，必须堵死）
                if self._wired_check is not None:
                    try:
                        if self._wired_check(name):
                            self._armed[name] = True
                            self._misses[name] = 0
                            return False
                    except Exception:  # noqa: BLE001 - 复核失败按未有线处理
                        pass
                self._armed[name] = False  # 单发：解除，等回升再布防
                self._misses[name] = 0
                # 全屏/游戏门控（2026-10-13）：投递前问一句。被抑制时**仍算
                # 已触发**（单发语义不变，不重复骚扰）—— 但也不补弹，理由见
                # 门控实现（补弹会在退出全屏时堆一屏通知，更烦）。
                if self._gate is not None:
                    try:
                        if not self._gate(name, pct):
                            return False
                    except Exception:  # noqa: BLE001 - 门控失败按"允许"处理
                        pass
                self._deliver(name, pct)
                return True
            return False

        # 读数高于阈值：撤销计数；回升到 threshold+gap 以上才重新布防
        self._misses[name] = 0
        if pct >= thr + self._reset_gap:
            self._armed[name] = True
        return False

    def _deliver(self, name: str, pct: int) -> None:
        self._last_notify_ts[name] = time.time()
        title = f"{name} 电量偏低"
        msg = f"当前 {pct}%，请及时充电"
        log.info("低电量提醒：%s = %d%%", name, pct)
        # 图标红字是主通道（agent._apply_icon）；toast 仅在 BATBAR_LOWBAT_TOAST=1 时补发
        if not toast_enabled():
            return
        if self._icon_getter is None:
            return
        icon = None
        try:
            icon = self._icon_getter()
        except Exception:  # noqa: BLE001
            pass
        if icon is None:
            return
        try:
            # pystray 各后端签名不一：win32 是 notify(title, msg)，
            # 有的要传回调；用 try 双签兜住。
            try:
                icon.notify(title, msg)
            except TypeError:
                icon.notify(title, msg, lambda *_: None)
        except Exception as exc:  # noqa: BLE001 - 弹通知失败绝不影响监控
            log.debug("notify failed: %s", exc)


# 进程级单例：agent 接线时 build 一次，各处喂读数。
_watcher: LowBatteryWatcher | None = None


def get(icon_getter=None, wired_check=None, gate=None) -> LowBatteryWatcher:
    global _watcher
    if _watcher is None:
        _watcher = LowBatteryWatcher(icon_getter=icon_getter, wired_check=wired_check, gate=gate)
    else:
        if icon_getter is not None and _watcher._icon_getter is None:
            _watcher._icon_getter = icon_getter
        if wired_check is not None and _watcher._wired_check is None:
            _watcher._wired_check = wired_check
        if gate is not None and _watcher._gate is None:
            _watcher._gate = gate
    return _watcher


def reset() -> None:
    """仅测试用：清空单例。"""
    global _watcher
    _watcher = None
