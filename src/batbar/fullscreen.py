"""全屏/游戏感知：判断「此刻该不该打扰用户」+ 该不该降频。

── 要解决的问题（ARCHITECTURE §6 待办）──
托盘 agent 会做两件"打扰性"的事：
  1. 低电量时弹 toast（`lowbattery._deliver`，`BATBAR_LOWBAT_TOAST=1` 才开）；
  2. 后台每 ~5s / ~9s / ~60s 各有一轮轮询，周期性读硬件、重绘图标。
用户在**玩游戏 / 看全屏视频 / 做演示**时，前者会突然冒出来打断，后者白烧
CPU（游戏已吃满资源）。系统其实**知道**用户正在全屏 —— 这就是本模块的信号源。

── 信号源：`SHQueryUserNotificationState`（Windows 官方"别打扰"判据）──
shell32 的这个导出是 Windows 自己决定"要不要弹通知"时用的权威 API，
返回 `QUNS_*` 枚举。本机实测（`probes/probe_quns.py` / `probe_quns_fullscreen.py`，
2026-10-13）：

    普通桌面（有焦点窗口）      → 5  QUNS_ACCEPTS_NOTIFICATIONS
    无边框全屏窗口获得焦点       → 2  QUNS_BUSY
    关掉全屏窗口                → 5  （即时恢复）

★ 关键实测结论：`QUNS` **能认无边框全屏**（游戏最常见的形态），返回 `BUSY`
  而不是 `D3D_FULL_SCREEN` —— 所以**不能只认 `QUNS_RUNNING_D3D_FULL_SCREEN`**，
  必须把整个"别打扰家族"都算进去（见 `is_dont_disturb`）。

── 各枚举值的语义（msdn: SHQueryUserNotificationState）──
    1 QUNS_NOT_PRESENT            屏保/锁屏中，用户不在
    2 QUNS_BUSY                   全屏应用正在跑（含无边框全屏）★
    3 QUNS_RUNNING_D3D_FULL_SCREEN 独占 D3D 全屏（真·全屏游戏）★
    4 QUNS_PRESENTATION_MODE      演示模式 / 投屏 ★
    5 QUNS_ACCEPTS_NOTIFICATIONS  正常，可以打扰
    6 QUNS_QUIET_TIME             系统"安静时间"（夜间）★
    7 QUNS_APP                    见 msdn（Win8+，应用态）★

── 分层（守项目铁律：纯逻辑与 I/O 分离）──
  * `is_dont_disturb` / `parse_mode` / `FullscreenGate` —— **纯逻辑**：喂状态
    值 / 喂时间，返回判定。可在任意平台离线单测。
  * `SHQueryUserNotificationState` 调用 —— Windows-only，在 agent 里做。

── 为什么要"迟滞"（`FullscreenGate`）──
QUNS 会随焦点微抖动（点一下桌面、弹个 UAC、切个窗口都可能瞬时变）。若直接
跟着走，会在游戏里反复"暂停→恢复→暂停"，每次恢复都真读一轮硬件 —— 比不做
还糟。故用**连续 N 次才翻面**的迟滞：进"别打扰"快（3 次 ≈ 6s，游戏一开始就
安静），出"别打扰"慢（5 次 ≈ 10s，避免刚切出去又切回来时白刷一轮）。
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("batbar.fullscreen")

# ── QUNS 枚举（msdn: SHQueryUserNotificationState）──
QUNS_NOT_PRESENT = 1                # 屏保/锁屏，用户不在
QUNS_BUSY = 2                       # 全屏应用（含无边框全屏）★ 本机实测命中
QUNS_RUNNING_D3D_FULL_SCREEN = 3    # 独占 D3D 全屏
QUNS_PRESENTATION_MODE = 4          # 演示/投屏
QUNS_ACCEPTS_NOTIFICATIONS = 5      # 正常，可打扰
QUNS_QUIET_TIME = 6                 # 系统安静时间
QUNS_APP = 7                        # Win8+ 应用态

# 全部"别打扰"取值。★ 特意含 BUSY 与 PRESENTATION_MODE：
# 实测无边框全屏返回的是 BUSY 而非 D3D_FULL_SCREEN，只认 D3D 会漏掉绝大多数游戏。
_DONT_DISTURB_STATES = frozenset({
    QUNS_NOT_PRESENT,
    QUNS_BUSY,
    QUNS_RUNNING_D3D_FULL_SCREEN,
    QUNS_PRESENTATION_MODE,
    QUNS_QUIET_TIME,
    QUNS_APP,
})

_STATE_NAMES = {
    QUNS_NOT_PRESENT: "NOT_PRESENT",
    QUNS_BUSY: "BUSY",
    QUNS_RUNNING_D3D_FULL_SCREEN: "D3D_FULL_SCREEN",
    QUNS_PRESENTATION_MODE: "PRESENTATION_MODE",
    QUNS_ACCEPTS_NOTIFICATIONS: "ACCEPTS_NOTIFICATIONS",
    QUNS_QUIET_TIME: "QUIET_TIME",
    QUNS_APP: "APP",
}

# ── 环境变量覆盖（排障 / 测试）──
#   BATBAR_FULLSCREEN=auto（默认）→ 真查 QUNS
#   BATBAR_FULLSCREEN=off         → 永不判定为"别打扰"（始终当正常）
#   BATBAR_FULLSCREEN=on          → 永远判定为"别打扰"（强制静音，验开关用）
ENV_MODE = "BATBAR_FULLSCREEN"
MODE_AUTO = "auto"
MODE_OFF = "off"
MODE_ON = "on"


def parse_mode(raw: str | None = None) -> str:
    """把 `BATBAR_FULLSCREEN` 的值归一成 auto/off/on（纯逻辑，可单测）。

    认不出的值一律回落 `auto` —— 宁可正常工作，不要因为环境变量打错字就
    把功能锁死。
    """
    if raw is None:
        raw = os.environ.get(ENV_MODE, "")
    v = (raw or "").strip().lower()
    if v in ("off", "0", "false", "no", "disable", "disabled"):
        return MODE_OFF
    if v in ("on", "1", "true", "yes", "force", "enabled"):
        return MODE_ON
    return MODE_AUTO


def state_name(state: int | None) -> str:
    """QUNS 值 → 可读名（日志/排障用）。未知值原样返回数字串。"""
    if state is None:
        return "None"
    return _STATE_NAMES.get(state, f"UNKNOWN({state})")


def is_dont_disturb(state: int | None) -> bool:
    """这个 QUNS 值算不算"别打扰"（纯逻辑，可单测）。

    注意：查询失败（`None`）时返回 **False** —— 读不到就当作"可以打扰"，
    宁可不静音（功能照常）也不要因为一次 API 抖动就永久静音。
    """
    if state is None:
        return False
    return state in _DONT_DISTURB_STATES


# ── 迟滞参数 ──
ENTER_STREAK = 3     # 连续 N 次"别打扰"才进入（进得快：游戏一开始就安静）
EXIT_STREAK = 5      # 连续 N 次"可打扰"才退出（出得慢：防抖，避免白刷硬件）
POLL_SECONDS = 2.0   # 查询节拍（2s × 3 = 6s 进入；2s × 5 = 10s 退出）


class FullscreenGate:
    """把抖动的 QUNS 采样整成稳定的「该不该静音/降频」开关（纯逻辑，可单测）。

    语义：
      * `sample(state, now)` 喂一次采样，内部按连续计数翻面；
      * `active` 属性 = 当前是否处于"别打扰"（已进入且未退出）；
      * `changed` 由 `sample` 返回，只在**翻面那一刻**为 True（供调用方打日志）。

    为什么不直接用"最近一次采样"：QUNS 会因焦点抖动（点桌面、切窗口）瞬时
    变化，跟着走会在游戏里反复"暂停→恢复"，每次恢复都真读硬件。
    """

    def __init__(self, enter_streak: int = ENTER_STREAK,
                 exit_streak: int = EXIT_STREAK,
                 mode: str | None = None) -> None:
        """
        Args:
            enter_streak: 连续多少次"别打扰"才进入。
            exit_streak:  连续多少次"可打扰"才退出。
            mode: auto/off/on；None = 从环境变量读。off/on 直接锁死状态、
                  不再看采样（排障用）。
        """
        self._enter = max(1, enter_streak)
        self._exit = max(1, exit_streak)
        self._mode = parse_mode(mode)
        self._active = False
        self._busy_streak = 0
        self._free_streak = 0
        self._last_state: int | None = None
        self._since = time.monotonic()
        # off/on 是"锁死"：进入初始态时就直接定好，sample() 不参与
        if self._mode == MODE_ON:
            self._active = True
        elif self._mode == MODE_OFF:
            self._active = False

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def locked(self) -> bool:
        """是否被环境变量锁死（与真实 QUNS 无关）。"""
        return self._mode in (MODE_ON, MODE_OFF)

    @property
    def active(self) -> bool:
        """当前是否处于"别打扰"（游戏/全屏/演示/安静时间）。"""
        return self._active

    @property
    def last_state(self) -> int | None:
        return self._last_state

    @property
    def since(self) -> float:
        """当前状态是何时进入的（monotonic），供日志算持续时长。"""
        return self._since

    def sample(self, state: int | None,
               now: float | None = None) -> bool:
        """喂一次 QUNS 采样。返回 **本次是否发生了翻面**。

        Args:
            state: QUNS 值；None = 查询失败（按"可打扰"处理，不静音）。
            now:   注入时间（单测用），None = time.monotonic()。
        """
        self._last_state = state
        if self.locked:
            return False        # off/on：状态已锁死，采样纯记录不参与判定

        now = time.monotonic() if now is None else now
        if is_dont_disturb(state):
            self._busy_streak += 1
            self._free_streak = 0
            if not self._active and self._busy_streak >= self._enter:
                self._active = True
                self._since = now
                return True
        else:
            self._free_streak += 1
            self._busy_streak = 0
            if self._active and self._free_streak >= self._exit:
                self._active = False
                self._since = now
                return True
        return False

    def describe(self) -> str:
        """一行状态摘要（日志用）。"""
        return (f"mode={self._mode} active={self._active} "
                f"last={state_name(self._last_state)} "
                f"busy0={self._busy_streak} free0={self._free_streak}")
