"""电量状态机（纯标准库，把 session 当鸭子类型依赖注入）。

职责：
  1. 轮询节奏：正常 60s 一次；设备睡眠（PING 超时）退避到 5min；醒来后 10s 内补读
  2. 通知缓存：Windows agent 把设备主动上报的电池通知透传过来，直接刷新缓存并置 dirty
  3. 蓝牙兜底：agent 定期跑 PowerShell 读 BTHENUM 电量属性，结果透传给本状态机
  4. 变化检测：percent/charging 变化或跨 5% 档位 → dirty → 触发重新渲染图标
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import hidpp as H

log = logging.getLogger("batbar.state")

POLL_ACTIVE = 60.0
POLL_SLEEPING = 300.0
WAKE_GRACE = 10.0
BT_POLL_EVERY = 300.0
CACHE_TTL = 30.0


@dataclass
class BatteryState:
    info: H.BatteryInfo | None = None
    sleeping: bool = False
    last_ok_ts: float = 0.0
    last_poll_ts: float = 0.0
    dirty: bool = True
    _last_render_key: tuple | None = field(default=None, repr=False)

    @property
    def percent(self) -> int | None:
        return self.info.percent if self.info else None

    def tooltip(self, device_name: str = "Logitech mouse") -> str:
        if self.sleeping or self.info is None:
            return f"{device_name}: 睡眠中/未知"
        p = self.percent
        # 读不出电量用 "--" 而不是 "?"（2026-10-13 用户定稿：任何时候不显示问号）
        s = f"{device_name}: {p}%" if p is not None else f"{device_name}: {self.info.approx or '--'}"
        if self.info.charging:
            s += "（充电中）"
        return s

    def render_key(self) -> tuple | None:
        if self.info is None:
            return None
        p = self.percent
        return (None if p is None else (p // 5) * 5, self.info.charging)


class BatteryPoller:
    """轮询调度核心 —— 同步 session 模式（Windows 直跑）。

    session：带 `read_battery()` 的对象（鸭子类型），`poll_once()` 同步读一步。
    bt_provider: () -> (percent, charging) | None，蓝牙兜底（Windows 侧 PowerShell）。

    旧桥接时代的**异步 RPC 模式**已于 v0.1.4 随桥接一起删除：`request_due()` /
    `mark_request_sent()` / `note_timeout()` 三件套与在途标记 `_awaiting`。
    controller 没了 ⇒ 没人会异步回灌 ⇒ 它们只剩定义、唯一调用方是单测
    （正是那种"被自己的测试保护着"的死代码）。不要从 git 历史里捞回来。
    """

    def __init__(self, session=None, bt_provider=None, now=time.monotonic):
        self._s = session
        self._bt = bt_provider
        self._now = now
        self.state = BatteryState()
        self._last_bt_ts = 0.0

    # ---- 外部事件 ----
    def on_notification(self, info: H.BatteryInfo) -> None:
        """设备主动上报（经 agent 透传）：最高优先，直接采信。"""
        self.state.info = info
        self.state.sleeping = False
        self.state.last_ok_ts = self._now()
        self._mark_dirty()

    def on_bt_battery(self, percent: int, charging: bool = False) -> None:
        """蓝牙兜底读数。仅当 HID++ 链路当前不可用（睡眠/读不到/未支持）才采信，
        避免双数据源打架。"""
        if self._hidpp_alive():
            return
        self.state.info = H.BatteryInfo(percent, None, charging, None, "bluetooth")
        self._mark_dirty()

    def _hidpp_alive(self) -> bool:
        return (
            self.state.info is not None
            and not self.state.sleeping
            and self.state.info.source != "bluetooth"
            and (self._now() - self.state.last_ok_ts) < POLL_SLEEPING * 2
        )

    # ---- 同步模式（Windows 直跑） ----
    def due(self) -> bool:
        t = self._now()
        interval = POLL_SLEEPING if self.state.sleeping else POLL_ACTIVE
        return t - self.state.last_poll_ts >= interval

    def poll_once(self) -> None:
        """同步读一步：记下轮询时刻 → 读 → 回灌状态机。"""
        if self._s is None:
            raise RuntimeError("BatteryPoller 需要 session（异步模式已随旧桥接删除）")
        self.state.last_poll_ts = self._now()
        try:
            info = self._s.read_battery()
        except Exception as exc:  # noqa: BLE001
            log.debug("read_battery failed: %s", exc)
            info = None
        self.poll_response(info)

    def poll_response(self, info: H.BatteryInfo | None, sleeping: bool | None = None) -> None:
        """把一次读的结果写进状态机（同步模式的收尾，也被通知/蓝牙路径复用）。

        Args:
            info: 电量读数；None 表示本次没读到
            sleeping: 仅当 info=None 时有意义——agent 已替我们判过睡眠
        """
        self.state.last_poll_ts = self._now()
        if info is not None:
            self.state.info = info
            self.state.sleeping = False
            self.state.last_ok_ts = self._now()
            self._mark_dirty()
            return
        if sleeping is True:
            self.state.sleeping = True
            self._maybe_bt()
            return
        # 读失败且没断言睡眠：保留旧值；连续失败视为 sleeping 退避以省 CPU
        if self.state.info is None:
            self.state.sleeping = True
        self._maybe_bt()

    def _maybe_bt(self) -> None:
        """设备睡眠/读不到时，若配置了蓝牙兜底且到期，则拉一次。"""
        t = self._now()
        if self._bt and (t - self._last_bt_ts) >= BT_POLL_EVERY:
            self._last_bt_ts = t
            try:
                r = self._bt()
                if r is not None:
                    self.on_bt_battery(*r)
            except Exception as exc:  # noqa: BLE001
                log.debug("bt provider failed: %s", exc)

    def _mark_dirty(self) -> None:
        key = self.state.render_key()
        if key != self.state._last_render_key:
            self.state.dirty = True

    def take_icon_update(self) -> BatteryState | None:
        """有变化返回当前 state 并清 dirty，否则 None。agent 渲染循环用。"""
        if self.state.dirty:
            self.state.dirty = False
            self.state._last_render_key = self.state.render_key()
            return self.state
        return None
