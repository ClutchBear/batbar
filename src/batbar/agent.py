"""Windows 侧 agent（原生 Windows Python，单进程全包）。

它做三件事，全在 Windows 侧完成、中间不需要任何 IPC：
  1. 用 hidapi 打开罗技 vendor collection，跑 HID++ 会话读鼠标电量
  2. 走 2.4G 私有通道 / USB 有线读机械键盘电量；走蓝牙读耳机/键盘电量
  3. 用 pystray 常驻系统托盘，Pillow 画好 PNG 直接换图；右键菜单切换显示哪台设备

**单进程**：读 USB、画图、贴托盘都在这里，中间不需要任何 IPC。早期版本曾把
「计算与渲染」拆到另一个进程用本地 TCP 桥相连（controller.py），实测没有收益
（Pillow 画 32×32 PNG 是微秒级，跨进程序列化反而多一层故障面），故已合并。

运行模式：
  --standalone      正常自跑：轮询→渲染→托盘（默认路径）
  --probe           只读一次电量打印结果就退出，验证 HID++ 协议对你的设备是否生效
  --scan            诊断：枚举所有罗技 collection 并逐槽位探测

线程模型：托盘 run() 占主线程（Windows 要求消息循环），各轮询/事件线程跑守护线程；
设备句柄只在持锁的 _ensure/_drop 里增删。

运行：
    python -m batbar.agent --standalone -v    # 前台调试
    python -m batbar.agent --probe            # 协议自检
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import socket
import struct
import threading
import time

from . import fullscreen as FS
from . import hidpp as H
from . import lowbattery
from . import transport as T
from . import wire
from .session import HidppSession, TransportClosed

log = logging.getLogger("batbar.agent")

# 启动计时（2026-10-13）：「双击后多久才看到托盘」这个问题一度只能靠肉眼猜，
# 而真正的耗时可能发生在 Python 之外（启动脚本、explorer 显示延迟）。这里把
# 进程内每个关键节点相对**模块导入**的耗时记进日志，下次一眼就能分清是
# 「进程起得慢」还是「explorer 显示慢」。
_BOOT_T0 = time.perf_counter()


def _boot(stage: str) -> None:
    """记一条启动打点（INFO 级，正式运行也看得到）。"""
    log.info("boot: %-22s +%.2fs", stage, time.perf_counter() - _BOOT_T0)


def anchor_cwd(target: str | None = None) -> str:
    """把进程工作目录锚定到一个**确定已知**的位置。

    为什么需要：agent 常由注册表 `Run` 键 / 计划任务 / `restart-diag.bat` 拉起，
    这些入口给的 cwd 往往是 `C:\\Windows\\System32` 或调用方所在目录。落盘路径
    如今统一由 `paths.py` 推导（打包后是 exe 同目录），**今天不受影响**；
    但只要将来有人写了 `open("config.json")` 这类相对路径，本机开发测试永远正常
    （cwd = 项目根），却只在用户开机自启时炸 —— 属于最难排查的一类故障。这一句
    就是廉价的保险。

    锚点选 `sys.base_prefix`（Python 安装根，如 `C:\\Python312`）而不是
    `os.getcwd()`：前者永远存在且绝不是 System32；`sys.base_prefix` 而非
    `sys.prefix`，因为在 venv 里 prefix 会指向 `.venv`（同样存在，但语义上
    安装根更稳定）。目标不存在时静默放弃，绝不因此让进程起不来。

    **frozen 环境（PyInstaller）另走一路**：那时 `sys.base_prefix` 被指到
    `sys._MEIPASS`（onefile 下是 `%TEMP%\\_MEIxxxxxx`）。锚在那里会**顶掉
    bootloader 的临时目录清理** —— Windows 不允许删除「正被某进程当作工作
    目录」的目录，于是 TEMP 里的 `_MEI*` 越堆越多。改为锚 exe 所在目录。

    参数 `target` 仅供测试注入。返回最终 cwd（失败时为原 cwd）。
    """
    import sys
    want = target
    if want is None:
        if getattr(sys, "frozen", False):
            # PyInstaller：见 docstring —— 锚 exe 所在目录，绝不碰 _MEIPASS。
            want = os.path.dirname(os.path.abspath(sys.executable))
        else:
            want = getattr(sys, "base_prefix", "") or ""
    try:
        if want and os.path.isdir(want):
            os.chdir(want)
    except Exception as exc:      # noqa: BLE001 —— 锚定失败不是致命错误
        log.debug("cwd 锚定失败（忽略）：%s", exc)
    try:
        return os.getcwd()
    except Exception:             # noqa: BLE001
        return want or ""


def attach_parent_console(attach: object = None) -> bool:
    """无控制台时，附加到调用方（cmd / 终端）的控制台。

    为什么需要：打包成 `--windowed` 的 exe 后 `sys.stdout` / `sys.stderr` 都是
    `None`。而 CPython 的 `print()` 对 `sys.stdout is None` 是**静默 no-op**
    （不抛异常！），也就是说 `batbar.exe --scan` 在 cmd 里跑会一个字都不打印，
    还以为是自己参数写错了 —— 比直接报错更难查。这里调 `AttachConsole` 把
    标准流接到调用者的控制台，让诊断模式在打包后依然可用。

    双击运行（没有父控制台）时 `AttachConsole` 失败 → 返回 False，一切照旧
    （日志照常落盘：打包后 = `batbar.exe` 同目录的 `logs\\agent.log`，
    见 `paths.py`）。

    必须早于 `applog.setup()`：安装器是靠「`sys.stdout` 是不是 None」来决定
    装不装控制台 handler 的，晚了就永远没有控制台输出。

    参数 `attach` 仅供测试注入（True/False 模拟附加结果）。永不抛异常。
    """
    import sys
    if sys.stdout is not None:
        return True                  # 源码运行 / console 版 exe：本来就有
    ok = False
    if attach is None:
        try:
            import ctypes
            # ATTACH_PARENT_PROCESS = -1
            ok = bool(ctypes.windll.kernel32.AttachConsole(-1))
        except Exception:            # noqa: BLE001
            ok = False
    else:
        ok = bool(attach)
    if not ok:
        return False
    try:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                          errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8",
                          errors="replace", buffering=1)
    except Exception:                # noqa: BLE001 —— 接不上就退化成"只有日志"
        return False
    return True


def make_console_safe() -> None:
    """让控制台输出**永不因编码而崩**。

    中文 Windows 的 `sys.stdout.encoding` 是 cp936(GBK)。往它写 emoji
    （✅/❌/⚠️）会抛 `UnicodeEncodeError` 并**直接终止进程** —— 2026-10-13
    打包 exe 时实测：`batbar.exe --scan` 跑到「✅ 可用写长度」那行就没了，
    rc=1，日志里一个字都没有（print 不走 logging，所以查不到）。而 Git Bash
    / VS Code 的 UTF-8 终端**永远复现不出来**，只在中文 cmd 与打包版上炸。

    这里是兜底层：把 errors 调成 `replace`，万一还有漏网字符最多显示成 `?`。
    真正的修复是把字符本身换成 GBK 可编码的（`_scan` 里已改 √ / ×）。
    """
    import sys
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")   # Python 3.7+ TextIOWrapper
        except Exception:            # noqa: BLE001 —— 换不了就算了，别因此起不来
            pass


def install_crash_log() -> None:
    """windowed exe 里未捕获异常会**凭空消失**，把它们钉进日志。

    没有控制台时 traceback 无处可去，用户看到的只是"跑一半没了"。上面那个
    `--scan` 的编码崩溃就是这么被藏住的。装个 excepthook，至少保证日志文件
    里有现场。

    必须在 `applog.setup()` **之后**调用 —— 之前装的话日志还没有文件 handler，
    写进去也是丢。
    """
    import sys
    import traceback

    def _hook(exc_type, exc, tb) -> None:
        try:
            log.error("未捕获异常，进程即将退出：\n%s",
                      "".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:            # noqa: BLE001
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook


HOST = "0.0.0.0"        # 监听全部入口（旧桥接遗留；主路径单进程自跑用不到）
PORT = 51788
READ_TTL = 25.0         # 电量缓存：轮询间隔内不重复戳硬件
MOUSE_FRESH = 8.0       # 罗技读数保鲜期（秒）——超过这么久没新读数，后台探测
                        # 主动重读一次，读不到就清掉条目。
                        # 2026-10-13 从 20s 收到 8s：实测（probes/probe_sleep_deep.py）
                        # 空闲 40s 的鼠标依然 0.20s 内应答 ⇒ 探测很便宜、也不会把
                        # 鼠标「戳醒」影响续航；而窗口越短，**关机后条目消失越快**
                        # （窗口 = 关机到下一次探测之间的最大延迟）。
# 启动时「等首帧内容就绪再注册托盘图标」的上限（秒）。见 `run()` 里的说明：
# shell 在 add 之后的一段时间里收下 NIM_MODIFY 却不重绘，而 add 那一帧必然
# 被渲染 —— 所以把真读数赶在 add 之前。鼠标在睡/没设备时最多等这么久。
BOOT_SHOW_WAIT = 1.2
ICON_SIZE = 32
# 手动切换设备后，图标显示它多久。**默认 None = 粘住不回退**（2026-10-12 用户
# 拍板：勾了 LEOBOG 结果 10 秒后图标自己跳回耳机，跟对勾完全对不上）。粘住期间
# 别的设备掉到低电也**不抢**图标；只有该设备从快照消失（拔线/断连/关机）才临时
# 回落自动规则，它一回来图标立刻跟回去。想恢复旧的「N 秒后自动交还」行为，用
# BATBAR_MANUAL_TTL 设一个秒数窗口。
MANUAL_TTL = (float(os.environ["BATBAR_MANUAL_TTL"])
              if os.environ.get("BATBAR_MANUAL_TTL") else None)


def _human_age(seconds: float) -> str:
    """把秒数说成人话（提示文案用）：<60s→「秒」，<60min→「分钟」，否则「小时」。"""
    if seconds < 60:
        return f"{int(seconds)} 秒"
    if seconds < 3600:
        return f"{int(seconds / 60)} 分钟"
    hours = seconds / 3600
    return f"{hours:.1f} 小时" if hours < 24 else f"{hours / 24:.1f} 天"


class _IcoBmpImage:
    """PIL Image 代理：pystray 内部 `image.save(f, format='ICO')` 默认写 PNG 内嵌
    条目，Windows LoadImage 对 PNG-ICO 的 alpha 处理在部分版本上直接涂黑（托盘
    黑方块根因）。这里拦截 save() 强制 bitmap_format='bmp'（32bpp BGRA DIB，
    alpha 通道走标准 GDI 路径）。其余属性透传给原图。"""

    def __init__(self, img):
        self._img = img

    def save(self, f, format=None, **params):  # noqa: A002
        if format and format.upper() == "ICO":
            params.setdefault("bitmap_format", "bmp")
        return self._img.save(f, format=format, **params)

    def __getattr__(self, name):
        return getattr(self._img, name)


def _install_menu_refresh(icon) -> None:
    """让托盘菜单在**每次弹出之前**重建（pystray 默认复用上次建好的 HMENU）。

    ── 为什么必须这样（2026-10-13 10:40 用户报「托盘已显示电量，右键菜单却还是
    『启动中…』」）──
    pystray 的右键处理是 `_win32.Icon._on_notify` → 直接
    `TrackPopupMenuEx(self._menu_handle)` —— **不重建菜单**，只弹上一次
    `update_menu()` 建好的那个 HMENU。所以「菜单内容永远最新」不是自动的：
    不重建就冻在上次构建那一刻。启动时那次构建必须走静态清单（否则它会去读
    硬件，实测 ~3s，把 NIM_ADD 拖到 3 秒后），于是**必须有人在弹出前把它换成
    真实清单** —— 就是这里。

    为什么挂 `_on_notify` 而不是定时重建：`TrackPopupMenuEx` 是**阻塞**的
    （菜单显示期间托盘线程停在里面），此时从别的线程 `update_menu()` 会把正在
    显示的 HMENU 销毁掉。在 `_on_notify` 里、`TrackPopupMenuEx` **之前**重建
    则完全没有这个问题（那一刻没有菜单在显示，且始终是托盘线程自己在做）。

    ⚠️ 实现细节：pystray 在 `__init__` 里就把 `self._on_notify`（**绑定方法**）
    存进了 `_message_handlers` 字典，窗口过程每次按字典查表 —— 所以**只改类属性
    没用**，必须把字典里那一条换掉（这里按 `__func__` 认人，不硬编码消息号）。
    """
    if getattr(icon, "_batbar_menu_refresh", False):
        return
    cls = type(icon)
    orig = getattr(cls, "_on_notify", None)
    handlers = getattr(icon, "_message_handlers", None)
    if orig is None or not isinstance(handlers, dict):
        # pystray 换了内部结构：别让托盘起不来，只是菜单不再自动刷新
        log.warning("pystray 后端结构不认识：菜单内容将不会随弹出刷新")
        return
    WM_RBUTTONUP = 0x0205

    def _on_notify(self, wparam, lparam):
        if lparam == WM_RBUTTONUP:
            try:
                self.update_menu()
            except Exception:         # noqa: BLE001 - 重建失败就用旧的，别影响弹出
                log.debug("menu refresh before popup failed", exc_info=True)
        return orig(self, wparam, lparam)

    for key, fn in list(handlers.items()):
        if getattr(fn, "__func__", None) is orig:
            handlers[key] = _on_notify.__get__(icon, cls)
            icon._batbar_menu_refresh = True
            return
    log.warning("pystray 消息表里没有 _on_notify：菜单内容将不会随弹出刷新")


class Agent:
    def __init__(self, device_hint: str | None = None):
        self._hint = device_hint
        self._session: HidppSession | None = None
        self._icon = None
        self._lock = threading.Lock()          # 护 _session
        self._clients_lock = threading.Lock()  # 护 _clients（独立锁：_broadcast 会在
        self._clients: list[socket.socket] = []  # session 锁内被 _on_notify 调用，与
                                                 # _ensure_device 的锁交叉会死锁）
        self._last_info: H.BatteryInfo | None = None
        self._last_read_ts = 0.0
        self._last_devnum: int | None = None       # 上次扫中的对码槽位（重开时快路径）
        self._last_icon = None                   # 最近一次 IconSpec（换色重绘用）
        self._last_tooltip = "Logitech"
        self._last_painted: tuple[bytes, str] | None = None   # 最近真正写进托盘的
                                                 # (png, tooltip)：内容没变就不打扰 shell
        self._running = True
        # 是否已拿到过真实读数（2026-10-13 用户需求：启动后立刻有图标，没数据
        # 就画 "--" 而不是先空白几秒、再画 "?"）。置位点见 _apply_icon（快照里
        # 出现任何非 None 读数）与 _bt_warm_loop（第一轮采集跑完）。
        self._ready = False
        # 托盘图标是否**真的注册进 shell**（`_setup` 里 `visible = True` 之后置位）。
        # 首次菜单构建会读一遍硬件（实测 ~3s），绝不能抢在 NIM_ADD 前面 ——
        # 2026-10-13 10:25 实测：等待首帧把 `_ready` 提前置位后，`run()` 里的
        # 首次菜单构建走了「真实清单」那条路，NIM_ADD 被拖到 +3.63s
        # （用户看到「四五秒后才出现电量数字」）。
        self._tray_shown = False
        # 图标/菜单更新串行化（pystray 句柄销毁+重建非原子；可重入，因为
        # _menu_items → _apply_icon → _set_icon 是同线程的一条链）
        self._icon_lock = threading.RLock()
        # ---- 多设备（罗技 HID++ + 蓝牙键盘/耳机）----
        self._bt = None                          # bt_battery.BluetoothBattery（懒建）
        self._tracked = "hidpp"                  # 当前跟踪源："hidpp" 或 "BT:<名>"
        self._tracked_pct: int | None = None     # BT 源的最近读数（standalone 渲染用）
        self._tracked_chg = False
        self._bt_misses = 0                      # BT 源连续读不到计数（自适应回通用）
        self._sleep_until = 0.0                  # HID++ 读失败后的退避截止（防 0.5s 循环狂戳硬件）
        self._last_dongle_refresh = 0.0          # 2.4G 帧回调触发的 UI 刷新节流
        self._lowbat = None                      # 低电量提醒状态机（run() 里建，懒加载）
        self._last_warn: tuple[str, int, bool] | None = None   # 图标选源缓存（变了才重绘）
        self._last_raw = (None, False)           # 非警告态的原始读数（换主题重绘用）
        self._manual_name: str | None = None     # 手动切换的显示设备（临时抢图标）
        self._manual_until = 0.0                 # 手动显示截止（到时自动回退低电设备）
        self._ticks = 0                          # 后台轮询计数（每 4 轮无条件重绘图标）
        self._warned: dict[str, float] = {}      # 节流告警时间戳（同一错误源 30s 一次）
        # 全屏/游戏感知（2026-10-13）：QUNS 采样 → 迟滞门 → 静音 toast + 暂停轮询
        self._fs_gate = FS.FullscreenGate()      # 纯逻辑，环境变量可锁死
        self._fs_skip_toast = 0                  # 因全屏而抑制的提醒计数（只统计）
        _theme_listeners.append(self._on_sys_theme)   # 任务栏换深浅 → 图标重绘
        _device_listeners.append(self._on_device_change)   # 设备插拔 → 立即重扫
        _taskbar_listeners.append(self._on_taskbar_recreated)  # explorer 重启 → 重注册
        _mouse_listeners.append(self._on_mouse_activity)   # 鼠标有活动 → 催一次探测

    # ---------- 设备 ----------
    def _ensure_device(self) -> HidppSession | None:
        with self._lock:
            if self._session is not None:
                return self._session
            cands = T.find_devices(bt_names=(self._hint,) if self._hint else ())
            if not cands:
                return None
            # 环境变量可手动锁定（配合 --scan 输出）：BATBAR_USAGE_PAGE / BATBAR_DEVNUM
            import os
            up_env = os.environ.get("BATBAR_USAGE_PAGE")
            if up_env:
                up = int(up_env, 16) if up_env.lower().startswith("0x") else int(up_env)
                cands = [d for d in cands if d.get("usage_page") == up] or cands
            devnum_env = os.environ.get("BATBAR_DEVNUM")

            # Bolt 实测（C53F）：0x10 短帧与 0x11 长帧的 input report 声明在同页
            # 不同 collection，Windows 按 report ID 分流 —— 必须「写 primary、
            # 读同页全部」。逐个打开，第一个写成功的当 primary。
            handles = []
            for d in cands:
                try:
                    handles.append((d, T.open_path(d["path"])))
                except Exception as exc:  # noqa: BLE001 - 被系统占用等
                    log.debug("open %s failed: %s", d.get("path"), exc)
            if not handles:
                return None
            primary_i, flen = -1, None
            for i, (_d, dev) in enumerate(handles):
                n = T.try_write(dev, H.build_write_frame(H.DEV_RECEIVER, bytes([0x00, 0x10, 0x00])))
                if n is not None:
                    primary_i, flen = i, n
                    break
            if primary_i < 0:
                for _d, dev in handles:
                    try:
                        dev.close()
                    except Exception:  # noqa: BLE001
                        pass
                log.warning("罗技 HID++ collection 均不可写（无 HID++ 入口？）")
                return None
            pd, pdev = handles[primary_i]
            readers = [dev for i, (_d, dev) in enumerate(handles) if i != primary_i]
            idx, _ = self._discover_devnum(pdev, readers, flen, devnum_env)
            sess = HidppSession(T.HidapiTransport(pdev, frame_len=flen, readers=readers),
                                device_index=idx)
            sess.set_notification_callback(self._on_notify)
            self._session = sess
            log.info("opened HID++ device: up=0x%X %s (device_index=%d, write=%dB, readers=%d)",
                     pd.get("usage_page", 0), pd.get("product_string") or pd["path"], idx, flen, len(readers))
            return sess

    def _discover_devnum(self, dev, readers, flen: int | None,
                         devnum_env: str | None) -> tuple[int, int | None]:
        """返回 (device_index, 写长度)。

        接收器把对码设备藏在槽位 1-7（0xFF 是接收器自己，无电池）。
        手动指定优先；否则逐槽位用 find_feature 探测（标准 feature call，可靠；
        不用 ping——Bolt 对 ping 回错误帧）。BT 直连设备走 0xFF。

        ⚠️ 唤醒预算（2026-10-12 修「接收器重新插上后罗技永不复活」）：
        插上接收器那一刻鼠标多半在睡，**第一枪只负责把它敲醒，回帧赶不上
        0.4s 窗口**——旧实现 retries=0 全空 → 回落 0xFF → 会话冻死在接收器
        本体上（它无电池特性，read_battery 永远 None）。现在：
          ① 快路径先试**上次成功的槽位**（对码号稳定），0.8s×2 枪覆盖唤醒；
          ② 快路径不中再全扫 1-7（每槽同样 2 枪预算）；
          ③ 命中即记回 `self._last_devnum`，供下次重建时走 ①。
        """
        tr = T.HidapiTransport(dev, frame_len=flen, readers=readers)
        if devnum_env is not None:
            return int(devnum_env), flen
        probe = HidppSession(tr, device_index=H.DEV_RECEIVER, timeout=0.4, retries=0)

        def _hit(idx: int, timeout: float, retries: int) -> bool:
            probe._devnum = idx
            probe._timeout = timeout
            probe._retries = retries
            probe._feat_cache.clear()
            probe._battery_feat = None
            try:
                return probe.battery_feature() is not None
            except TransportClosed:
                raise

        # ① 快路径：记住的槽位（唤醒+命中双预算）
        remembered = getattr(self, "_last_devnum", None)
        if remembered is not None:
            try:
                if _hit(remembered, 0.8, 1):
                    return remembered, tr._frame_len
            except TransportClosed:
                return H.DEV_RECEIVER, tr._frame_len
        # ② 全扫 1-7
        for idx in (1, 2, 3, 4, 5, 6, 7):
            try:
                if _hit(idx, 0.8, 1):
                    self._last_devnum = idx
                    return idx, tr._frame_len
            except TransportClosed:
                break
        # 没扫到对码槽位：退回 0xFF（多半是 BT 直连，或鼠标在睡/关机）
        return H.DEV_RECEIVER, tr._frame_len

    def _drop_device(self) -> None:
        with self._lock:
            if self._session:
                try:
                    self._session.close()
                except Exception:  # noqa: BLE001
                    pass
                self._session = None

    def _on_notify(self, info: H.BatteryInfo) -> None:
        """设备主动上报（HID++ notification，省电模式下设备自己推）。

        ⚠️ percent=None 的通知**不写 `_last_info`**（2026-10-13）：那会在
        快照里留下一条「鼠标 罗技 G304: None」幽灵条目（菜单多一行、图标
        落到"全读不出"分支），而设备可能压根没连。转发消息不受影响。
        """
        if info.percent is not None:
            self._last_info, self._last_read_ts = info, time.monotonic()
        msg = wire.encode_msg("battery", source=info.source, percent=info.percent,
                              charging=info.charging, state="ok")
        self._broadcast(msg)

    # ---------- 罗技在/离线探测（不应答 = 不在） ----------
    def _read_with_second_chance(self, sess) -> H.BatteryInfo | None:
        """读一次；不应答就**立刻再读一次**（挡单次毛刺），仍不应答返回 None。

        ── 为什么判据是「不应答 = 不在」（2026-10-13 11:00 实测纠正）──
        曾经以为「不应答 = 睡眠」（01:52 那次误判），于是真槽位不应答就保留
        条目。今天用 `probes/probe_sleep_deep.py` 实测推翻了它：**静置 40s 的
        G304 依然在 0.20s 内应答**（空闲 ≠ 不应答，HID++ 请求本身就能把它叫
        醒）；而接收器寄存器 0x02 的「已连接数」在鼠标关机后也**不掉**
        （日志里从来没有 `1 → 0`）—— 两个「睡眠判据」都不成立。
        结论：真槽位不应答就是**不在**（关机/离队），只是先给一次复读机会，
        挡住单次毛刺（01:52 那次图标有 90、菜单却空，就是这么来的）。
        """
        info = sess.read_battery()
        if info is not None and info.percent is not None:
            return info
        info = sess.read_battery()
        if info is not None and info.percent is not None:
            log.info("罗技读数第二次才应答（单次毛刺）")
            return info
        return None

    def _probe_mouse(self) -> None:
        """罗技条目的活性判定（2026-10-12 用户需求：鼠标关机/接收器拔掉时
        不再显示罗技电量）。由 `_bt_warm_loop` 每轮（~9s）调用，内部自判
        新鲜度与退避，无需调用方过滤。

          * 读数距今 ≤ `MOUSE_FRESH` 秒（通知帧/轮询刚喂过）→ 免探；
          * 过期 → `read_battery()` 当场探（含一次复读，见
            `_read_with_second_chance`）：有应答刷新缓存；两次都无应答 →
            **清掉 `_last_info`**，条目从菜单与图标消失——与 2.4G 键盘
            「休眠与关机同权」口径一致。鼠标重新开机后下一轮探测即复活
            （G304 这类 Unifying 设备对 ping 不回帧，**不能拿 ping 当判据**，
            read_battery 才是可靠探针）；
          * 0xFF 回落会话（扫不到对码槽位）额外**丢会话重扫**——不丢就冻死在
            接收器本体上（2026-10-12「插上后刷新多次都不复活」）；
          * 句柄失效（TransportClosed）/枚举不到 = 拔接收器的典型路径 →
            丢会话 + 清读数，下轮重新枚举（插上即自愈）。
        任何失败统一退避 30s（`_sleep_until`，与 `_tracked_read` 共用），
        不狂戳硬件——没有罗技设备的机器也只每 30s 空枚举一次。
        """
        now = time.monotonic()
        if now < self._sleep_until:
            return                                   # 退避期内不重复戳
        if (self._last_info is not None
                and now - self._last_read_ts <= MOUSE_FRESH):
            return                                   # 读数还新鲜，免探
        try:
            sess = self._ensure_device()
            if sess is None:             # 枚举不到 = 接收器已拔/驱动节点消失
                self._last_info = None
                self._sleep_until = now + 30.0
                return
            info = self._read_with_second_chance(sess)
        except TransportClosed:
            self._drop_device()
            self._last_info = None
            self._sleep_until = now + 30.0
            return
        except Exception as exc:         # noqa: BLE001 - 探测失败按不应答处理
            log.debug("mouse probe: %s", exc)
            self._last_info = None
            self._sleep_until = now + 30.0
            return
        if info is not None:
            if self._last_info is None:
                log.info("罗技条目出现：%d%%", info.percent)
            self._last_info, self._last_read_ts = info, now
            self._sleep_until = 0.0
            return
        # 两次都不应答 = 设备真不在（关机 / 走远）。
        if sess._devnum == H.DEV_RECEIVER:
            # 0xFF 回落会话 = 接收器本体（**扫不到任何对码设备**）。
            # ⚠️ 必须**丢会话重扫**：0xFF 永远读不出电池，不丢就冻死在这条
            # 死会话上（2026-10-12「插上后刷新多次都不复活」）。
            self._drop_device()
            if self._last_info is not None:
                log.info("罗技条目隐藏：接收器扫不到对码设备（鼠标关机/空槽）")
        elif self._last_info is not None:
            log.info("罗技条目隐藏：槽位 %d 两次读取都无应答（鼠标关机）",
                     sess._devnum)
        self._last_info = None
        self._sleep_until = now + 30.0

    # ---------- 多设备跟踪 ----------
    def _bt_battery(self):
        if self._bt is None:
            from .bt_battery import BluetoothBattery
            self._bt = BluetoothBattery()
        return self._bt

    # ---------- 低电量提醒接线 ----------
    def _lowbat_init(self):
        """建/取低电量状态机单例。

        icon_getter 延迟到弹时再取托盘；wired_check 是给状态机的「弹前实时
        复核」闭包 —— 它要起 USB 枚举，所以只在即将弹的那一瞬调用一次，
        绝不在常规 feed 里跑（那次用的是 TTL 缓存值）。
        """
        try:
            if self._lowbat is None:
                from . import receiver24 as R

                def _real_wired(name: str) -> bool:
                    # 状态机传进来的是显示名，可能带 (2.4G)/(有线) 后缀；
                    # 只有已知目标才可能有线，其余直接 False（省一次枚举）。
                    if R.base_name(name) not in R.KNOWN_RECEIVERS.values():
                        return False
                    return R.is_wired(name, force=True)

                self._lowbat = lowbattery.get(icon_getter=lambda: self._icon,
                                              wired_check=_real_wired,
                                              gate=self._gate_notify)
            return self._lowbat
        except Exception as exc:  # noqa: BLE001 - 提醒失败不影响监控
            log.debug("lowbat init: %s", exc)
            return None

    def _feed_lowbat(self, name: str, pct: int | None,
                     charging: bool = False) -> None:
        """喂一条读数给低电量状态机（廉价：有线态吃 TTL 缓存，不 force）。

        状态机内部一律用剥后缀基名做 key —— 显示名会随连接方式在
        「LEOBOG 键盘 (2.4G)」/「(有线)」/蓝牙名之间变，计数不能跟着断。
        真正的「弹前实时复核」由状态机调 wired_check 闭包完成，这里不
        重复起 USB 枚举 —— 常规刷新每 2s 就来一条，每次都枚举会拖慢托盘。
        """
        try:
            w = self._lowbat_init()
            if w is None:
                return
            from . import receiver24 as R
            from . import wired24
            base = R.base_name(name)
            wired = base in R.wired_bases()   # 吃缓存
            # 插线充电 = 安全态：有线通道的充电标志也喂进去，免得插着线报低电
            if wired and wired24.charging(base):
                charging = True
            # key 一律用剥后缀的基名：同一张键盘在 (2.4G)/(有线)/蓝牙三种
            # 显示名之间切换时，去抖计数与布防态必须连续，不能各算各的。
            w.feed(base, pct, charging, wired)
        except Exception as exc:  # noqa: BLE001
            log.debug("lowbat feed: %s", exc)

    def _feed_snapshot(self) -> None:
        """把整张快照批量喂给低电量状态机（后台线程用，覆盖全部设备）。

        只喂跟踪源会漏掉其余设备 —— 提醒关心的是「任何一台」低电，不是
        「图标正在显示的那台」。BT 读数用缓存快照，不额外戳硬件。
        """
        try:
            snap = self.devices_snapshot()
        except Exception:  # noqa: BLE001
            return
        for n, p in snap.items():
            # 跟踪源如果就是快照里这条，带上充电态（罗技 HID++ 报告里有）
            chg = (n == self._tracked_name() and self._last_info is not None
                   and bool(self._last_info.charging))
            self._feed_lowbat(n, p, chg)

    def _warn_throttled(self, key: str, msg: str, exc: BaseException) -> None:
        """同一错误源最多每 30s 提醒一次（WARNING 级）。

        教训（2026-10-12）：`devices_snapshot` 里各数据源都用 `log.debug` 吞异常，
        结果 wired24 的 ctypes 声明冲突让 2.4G 条目整条消失、日志里却只有 DEBUG
        行，用户只看到「设备不见了」。改成节流 WARNING：不刷屏，但看得见。
        """
        now = time.monotonic()
        if now - self._warned.get(key, 0.0) >= 30.0:
            self._warned[key] = now
            log.warning("%s: %s", msg, exc)

    def devices_snapshot(self) -> dict[str, int | None]:
        """{显示名: 电量%|None}，含 HID++ 主设备、全部蓝牙设备、2.4G 键盘。

        菜单/桥共用。2.4G 条目由 receiver24 补：某张键盘此刻不在蓝牙列表里
        （说明切到 2.4G 接收器了，那条链路不上报电量），才用 lastknown 里它
        上一次在蓝牙上的读数兜底，并带 " (2.4G)" 后缀标明连接方式。
        """
        out: dict[str, int | None] = {}
        # ⚠️ percent 也必须是真读数（2026-10-13）：`_last_info` 可能持有
        # percent=None 的 BatteryInfo（设备在但报不出电量，如刚上电/接收器
        # 空槽）。只判 `is not None` 会造出一条「鼠标 罗技 G304: None」的
        # **幽灵条目** —— 菜单里显示「鼠标 罗技 G304 ?%」、图标选源走到
        # 「有条目但全读不出」分支画 "?"，而设备其实根本没连。
        if self._last_info is not None and self._last_info.percent is not None:
            out["鼠标 罗技 G304"] = self._last_info.percent
        try:
            # cached_devices：不吃 4s 的同步延迟，陈旧值由后台线程焐热刷新
            out.update(self._bt_battery().cached_devices())
        except Exception as exc:  # noqa: BLE001 - CIM 查询失败不影响主设备
            self._warn_throttled("bt", "蓝牙快照失败（蓝牙条目会缺失）", exc)
        try:
            from . import receiver24 as R
            r24 = R.devices(frozenset(out))
            # 键盘本体插着 USB 线时，菜单条目从「(2.4G)」改标「(有线)」——
            # 同厂商直连 USB 设备出现 = 线插着（dongle 通常同时在线，无线帧
            # 仍可能实时；但「有线」对用户更有信息量：既在充电也可有线用）。
            # 显示名整体替换为 wired_bases() 的判据，剥后缀查基名。
            wired = R.wired_bases()
            fixed: dict[str, int | None] = {}
            for disp, pct in r24.items():
                if R.base_name(disp) in wired:
                    fixed[disp[: -len(R.SUFFIX)] + R.WIRED_SUFFIX] = pct
                else:
                    fixed[disp] = pct
            out.update(fixed)
        except Exception as exc:  # noqa: BLE001 - HID 枚举失败不影响其余设备
            self._warn_throttled("r24", "2.4G 快照失败（2.4G 条目会缺失）", exc)
        # 有线通道兜底：接收器没插（或没枚举到）但键盘插着数据线时，
        # 「关闭档 + 线」这种无线全死的组合也要能显示（2026-10-12）。
        try:
            from . import receiver24 as R
            from . import wired24
            from .dongle import TARGETS
            have = {R.base_name(n) for n in out}
            for name in set(TARGETS.values()):
                if name in have or not wired24.plugged(name):
                    continue
                out[name + R.WIRED_SUFFIX] = wired24.battery(name)
        except Exception as exc:  # noqa: BLE001
            self._warn_throttled("wired", "有线快照失败（有线条目会缺失）", exc)
        return out

    def _tracked_read(self) -> tuple[int | bool | None, bool, str]:
        """读当前跟踪源的电量 → (percent|None, charging, 状态)。

        状态: ok / sleeping / error。HID++ 失败退避 30s（standalone 0.5s 循环
        不能每秒戳睡眠设备）；BT 源走 TTL 缓存的 CIM 查询，天然节流。"""
        now = time.monotonic()
        if self._tracked.startswith("24G:"):
            # 2.4G 接收器厂商通道（迈从/LEOBOG dongle）：电量来自事件驱动
            # 推送帧（开机/插拔充电/灯变化），静默期吃 lastknown 存档兜底，
            # 两者都没有 = 键盘还没发过帧（视为 sleeping，图标保留旧值）。
            base = self._tracked[4:]
            from . import dongle as _dg
            from . import lastknown as _lk
            pct = _dg.battery(base)
            if pct is not None:
                return pct, False, "ok"
            got = _lk.get(base)
            if got is not None:
                return got[0], False, "ok"
            return None, False, "sleeping"
        if self._tracked == "hidpp":
            if self._last_info is not None and now - self._last_read_ts < READ_TTL:
                return self._last_info.percent, self._last_info.charging, "ok"
            if now < self._sleep_until:
                return (self._last_info.percent if self._last_info else None,
                        bool(self._last_info and self._last_info.charging), "sleeping")
            sess = self._ensure_device()
            if sess is None:
                self._sleep_until = now + 30.0
                return None, False, "error"
            try:
                info = sess.read_battery()
            except TransportClosed:
                self._drop_device()
                self._sleep_until = now + 30.0
                return None, False, "error"
            if info is None or info.percent is None:
                # ⚠️ percent=None 的 info **不许进 `_last_info`**（2026-10-13）：
                # 那会在快照里造出一条值为 None 的「鼠标 罗技 G304」幽灵条目，
                # 让菜单多一行 "鼠标 罗技 G304 ?%"、让图标落到「全读不出」
                # 分支 —— 设备明明没连却像在。读不出就是读不出，按 sleeping
                # 处理，保留上一次**真读数**（若有）。
                self._sleep_until = now + 30.0
                return (self._last_info.percent if self._last_info else None,
                        bool(self._last_info and self._last_info.charging), "sleeping")
            self._last_info, self._last_read_ts = info, now
            return info.percent, info.charging, "ok"
        # 蓝牙源："BT:<名>"。自适应：设备切走（如键鼠改 2.4G、耳机断连）后
        # 会从过滤结果消失——连续两轮（>TTL*2）不在列表就回落 HID++ 主设备，
        # 避免图标永远冻在最后一次读数上。
        name = self._tracked[3:]
        devs = self._bt_battery().all_devices()
        pct = devs.get(name)
        if pct is None:
            self._bt_misses += 1
            if self._bt_misses >= 3:
                self._bt_misses = 0
                self._set_tracked("hidpp")
                return self._tracked_read()
            return self._tracked_pct, self._tracked_chg, "sleeping"
        self._bt_misses = 0
        self._tracked_pct, self._tracked_chg = pct, False
        return pct, False, "ok"

    def _set_tracked(self, device: str, *, snap: dict | None = None) -> None:
        """切换跟踪源。`snap` = 菜单点击时传入的**弹菜单那一刻的同一份快照**：
        图标刷新直接吃它，不再异步重读 —— 修「菜单行 100、点下去图标变 99」
        （2026-10-12 22:32 用户报）。不带 snap 的调用方（bridge 指令、启动）
        仍走 `_tracked_read()` 现读。"""
        self._tracked = device
        self._bt_misses = 0
        log.info("tracking %s", device)
        self._broadcast(wire.encode_msg("tracking", device=device))
        # 手动切换：图标显示这台（用户需求 2026-10-12）。MANUAL_TTL=None 时
        # **粘住不回退**——用户点谁就盯谁，直到点另一台或它从快照消失；
        # 设了 BATBAR_MANUAL_TTL 才恢复旧的「N 秒后交还自动规则」行为。
        name = self._tracked_name()
        if snap is not None and name not in snap:
            # 形态后缀在「弹菜单→点下去」之间翻转了（如拔掉线，(有线)→(2.4G)）：
            # 换另一种后缀在快照里找，找不到再按原样（自动规则会兜底）。
            from . import receiver24 as R
            alt = {R.SUFFIX: R.WIRED_SUFFIX, R.WIRED_SUFFIX: R.SUFFIX}
            for suf, other in alt.items():
                if name.endswith(suf) and (name[: -len(suf)] + other) in snap:
                    name = name[: -len(suf)] + other
                    break
        self._manual_name = name
        self._manual_until = (time.monotonic() + MANUAL_TTL
                              if MANUAL_TTL is not None else float("inf"))
        self._schedule_manual_revert()
        # 切源后立刻让 UI 反映新读数（旧 bridge 模式下外部会重推图标）
        if self._icon:
            if snap is not None:
                threading.Thread(target=self._apply_icon,
                                 args=(snap.get(name), False, snap),
                                 daemon=True).start()
            else:
                threading.Thread(target=self._refresh_from_tracked,
                                 daemon=True).start()

    def _schedule_manual_revert(self) -> None:
        """到点把图标交还给自动规则（低电设备长显）。

        必须独立计时：`_bt_warm_loop` 一轮 ~9s，比 10s 的手动窗口还长，
        靠它轮询会晚好几秒才回退，用户会觉得「切了没反应/回不去」。

        粘住模式（MANUAL_TTL=None，默认）不排这个定时器——图标就固定在用户
        点的那台，只有它从快照消失才由 `_icon_target` 临时回落自动规则。
        """
        if MANUAL_TTL is None:
            return
        deadline = self._manual_until

        def _run() -> None:
            time.sleep(max(0.2, deadline - time.monotonic()) + 0.3)
            if time.monotonic() < self._manual_until:
                return                      # 期间又切过，交给新的计时线程
            self._manual_name = None
            log.debug("manual icon window over -> back to auto")
            try:
                self._refresh_from_tracked()
            except Exception as exc:  # noqa: BLE001
                log.debug("manual revert refresh: %s", exc)

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_from_tracked(self) -> bool:
        """读一次跟踪源并重绘图标；返回**是否真的画上去了**。

        返回值很关键：托盘还没建好时 `_set_icon` 是空操作，调用方（后台轮询）
        必须据此决定要不要把「已刷新」缓存写上——否则会出现「启动后图标一直
        停在初始值」的 bug（2026-10-12 实测：warm loop 先于 _build_tray 启动）。
        """
        pct, chg, _st = self._tracked_read()
        # 启动首帧加速（2026-10-13 用户需求「启动后立刻显示电量」）：跟踪源已经
        # 读到数就用「只含它一台」的快照立刻画，不排队等蓝牙全量 CIM 查询（首次
        # 实测 4~25s）——否则明明鼠标电量 1 秒内就有了，图标却要陪蓝牙一起等。
        # 完整快照随后由 _bt_warm_loop 那轮补上（那时会重新走一遍选源规则）。
        # ⚠️ 只在这一次「还没就绪且非手动选源」时走快路径，避免打扰用户的选择。
        snap = None
        if pct is not None and not self._ready and not self._manual_name:
            try:
                snap = {self._tracked_name(): pct}
            except Exception:                # noqa: BLE001 - 算不出名字就走常规路径
                snap = None
        return self._apply_icon(pct, chg, snap)

    def _tracked_name(self) -> str:
        """当前跟踪源的显示名（tooltip/手动选源用）：hidpp→鼠标 罗技 G304，
        24G:<基名>→基名+(2.4G) 或 +(有线)，BT:<名>→设备名。

        ⚠️ 24G 源的后缀必须**跟着当前连接形态走**（2026-10-12 修「有线条目
        点了无法勾选」）：`devices_snapshot` 会把插线键盘的条目从 `(2.4G)`
        改写成 `(有线)`，若这里恒定拼 `(2.4G)`，手动选源在快照里查无此名 →
        `_icon_target` 的手动分支永远不命中 → 对勾纹丝不动。
        """
        if self._tracked == "hidpp":
            return "鼠标 罗技 G304"
        if self._tracked.startswith("24G:"):
            from . import receiver24 as R
            base = self._tracked[4:]
            try:
                if R.is_wired(base):
                    return base + R.WIRED_SUFFIX
            except Exception:  # noqa: BLE001 - 判不了有线就按 2.4G 显示
                pass
            return base + R.SUFFIX
        return self._tracked[3:]

    def _tip_for(self, pct: int | None, charging: bool,
                 name: str | None = None) -> str:
        """文案格式对齐系统托盘原生提示（如「耳机 (蓝牙): 69%」）：
        走原生 szTip，Win11 shell 自动渲染成同款深色气泡/字体/字号。

        2.4G/有线条目（receiver24 兜底）不是稳定实时读数，明确标出来，
        不冒充实测值。休眠 = 省电静默（仍有电、一碰就醒），和「离线」不是一回事。

        Args:
            name: 显示哪台设备；None = 跟踪源（旧行为）。图标现在显示的是
                **电量最低的设备**，未必是跟踪源，所以必须能指定。
        """
        name = name or self._tracked_name()
        suffix = ""
        try:
            from . import dongle, lastknown
            from . import receiver24 as R
            from . import wired24
            base = R.base_name(name)
            # 凡带 2.4G/有线后缀的条目都可能吃存档值，判定按「后缀」不按
            # is_stale_entry —— 有线条目名字换成了 (有线)，后缀判定会漏它。
            if name.endswith(R.WIRED_SUFFIX):
                # 插着数据线：标明「有线」（是否充电由下面的通用后缀给出）
                suffix = " · 有线"
            elif name.endswith(R.SUFFIX):
                live = dongle.battery(base)
                if live is not None and (pct is None or live == pct):
                    suffix = " · 2.4G 实时"
                    if R.is_sleeping(name):
                        suffix += " · 休眠中"
                else:
                    got = lastknown.get(base)
                    if got is not None:
                        suffix = f" · 上次读数 {_human_age(time.time() - got[1])}前"
        except Exception:  # noqa: BLE001
            pass
        if pct is None:
            return f"{name}: 未知{suffix}"
        s = f"{name}: {pct}%"
        if charging:
            s += " (充电中)"
        return s + suffix

    def _icon_target(self, snap: dict | None = None) -> tuple[str, int, bool] | None:
        """托盘图标该显示哪台设备 → (显示名, pct, 是否警告)。

        规则（2026-10-12 用户拍板，三段式）：
          1. **手动选择粘住**：菜单点了某台 → 图标显示它，**直到点另一台或它
             从快照消失**（拔线/断连/关机才临时回落自动规则；设了
             `BATBAR_MANUAL_TTL` 才有 N 秒窗口自动交还）；
          2. **低电长显**：有设备 ≤ 阈值（默认 20%）→ 显示其中电量最低的那台
             并变红（有线/充电中除外，插着电不算「需要关注」）；
          3. 没有低电设备 → **优先显示电量最低的耳机**（耳机不盯着容易突然没电）；
          4. 连耳机都没有 → 显示快照里**任意有读数设备中电量最低的一台**
             （有线/充电不再排除）——修「只插键盘时图标永远问号」。
        0%/None 一律排除（2.4G 链路毛刺常吐假 0）。

        Args:
            snap: 复用调用方已读好的设备快照。⚠️ 菜单构建必须传（2026-10-12
                修「托盘 91 但对勾行 92」）：2.4G 电量帧是 ±2 抖动的 ADC，
                同一次弹菜单里读两份快照会跨帧取到不同值 → 对勾行数字与
                图标数字对不上。传同一份 snap 让标签/对勾/图标三处同源。
        """
        try:
            from . import lowbattery as LW
            from . import receiver24 as R
            if snap is None:
                snap = self.devices_snapshot()
            if not snap:
                return None
            wired = set(R.wired_bases())
            # 快照 key 是显示名（可能带 (2.4G)/(有线) 后缀），先剥后缀再选
            bases = {n: R.base_name(n) for n in snap}
            flat = {bases[n]: snap[n] for n in snap}
            charging = set()
            tn = self._tracked_name()
            if tn in snap and self._last_info is not None and self._last_info.charging:
                charging.add(bases.get(tn, tn))

            thr = LW.threshold()
            # ① 手动窗口内：直接显示用户点的那台。⚠️ 读数 None 也显示
            #    （图标画 "?"）—— 关机充电的有线条目就是没读数，但用户点了
            #    它，对勾必须跟过去；「0/None 排除」只约束自动规则（防毛刺
            #    抢图标），不该拦手动选择（2026-10-12 修「有线条目点了没反应」）。
            if self._manual_name and time.monotonic() < self._manual_until:
                m = self._manual_name
                if m in snap:
                    p = snap[m]
                    return (m, p, p is not None and p <= thr)

            # ② 低电长显（最低的那台低电设备）
            hit = LW.pick_low(flat, thr, wired, charging)
            if hit is None:
                # ③ 无低电设备 → 优先显示电量最低的**耳机**（用户需求「启动时
                #    优先显示电量低的耳机」）。
                hit = LW.pick_preferred(flat, wired, charging)
                if hit is None:
                    # ④ 终极兜底（2026-10-12 用户报「插上键盘刷新后列表有了、
                    #    托盘还是问号」）：快照里**任何一台有读数的设备**都该
                    #    显示——②③的排除是「谁有资格抢图标/报警」的规则，
                    #    不该滑成「明明有设备图标却永远问号」。先挑非有线非
                    #    充电里的最低者（维持「插着电不抢」的语义），一台都
                    #    没有（纯插线键盘）才轮到有线/充电设备本身。
                    hit = LW.pick_display(flat, wired, charging)
                    if hit is None:
                        hit = LW.pick_display(flat)
                    if hit is None:
                        return None
                warn = False
            else:
                warn = True
            for n in snap:                       # 还原完整显示名（tooltip 用）
                if bases[n] == hit[0]:
                    return (n, hit[1], warn)
            return (hit[0], hit[1], warn)
        except Exception as exc:  # noqa: BLE001 - 扫描失败按无目标处理
            log.debug("icon target scan: %s", exc)
            return None

    def _is_charging(self, name: str) -> bool:
        """该显示名对应的设备此刻是否在充电（图标画 ⚡ 用）。

        两条来源：① 有线通道的充电标志（插线键盘，含关闭档/2.4G 档）；
        ② 罗技 HID++ 报告的充电位（跟踪源就是它时）。
        """
        try:
            from . import receiver24 as R
            from . import wired24
            base = R.base_name(name)
            if wired24.plugged(base) and wired24.charging(base):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            if name == self._tracked_name() and self._last_info is not None:
                return bool(self._last_info.charging)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _apply_icon(self, pct: int | None, charging: bool,
                    snap: dict | None = None) -> bool:
        """直驱托盘画图（旧 bridge 模式由外部推 icon 消息，不走这；
        那种模式下图标配色不随任务栏深浅——PNG 是外部画的）。

        图标选源 = `_icon_target()` 三段式（2026-10-12 用户拍板）：
        手动选择（默认粘住）> 低电设备长显（≤阈值变红）> 最低耳机 > 跟踪源。
        返回是否真的画上去了（托盘未建好时为 False，调用方据此重试）。
        `snap` 透传给 `_icon_target`：菜单打开时传同一份快照，图标数字与
        菜单行数字保证同源（同一次读取，不被 ADC 抖动劈成两个值）。
        """
        from . import icons as I
        try:
            self._last_raw = (pct, charging)
            light = not _detect_sys_dark()
            # 快照只读一次、一路传下去（与菜单同一份时图标/列表必然同源）。
            if snap is None:
                snap = self.devices_snapshot()
            # 有了任何真读数就算「已就绪」（此后再空 = 设备真不在，不再是启动态）
            if any(v is not None for v in snap.values()):
                first = not getattr(self, "_ready", False)
                self._ready = True
                if first:
                    _boot("首个真读数已画")
            tgt = self._icon_target(snap)
            if tgt is not None:
                tname, tpct, warn = tgt
                # 插着数据线的键盘 → 图标带 ⚡（复用既有充电闪电设计）
                chg = self._is_charging(tname)
                spec = I.IconSpec(tpct, chg, ICON_SIZE,
                                  light_taskbar=light, warning=warn)
                tip = (f"⚠ {tname} 电量偏低: {tpct}%" if warn
                       else self._tip_for(tpct, chg, name=tname))
            elif not snap or not self._ready:
                # 快照空 / **还没拿到过任何真读数** = 一台设备都不在或还在启动。
                # 此时显示「跟踪源未知」纯属误导——跟踪源那台根本不在（2026-10-12
                # 用户报「全拔掉 tooltip 显示 鼠标 罗技 未知」）→ 如实报未发现。
                # 图标画 "--"（2026-10-13 用户定稿：比 "?" 观感好）。
                # ⚠️ 启动阶段（`not _ready`）也走这里：旧实现建托盘时是张全透明
                # 空图，要等后台读完蓝牙（首次 CIM 查询 4~25s）才有内容 → 头几秒
                # 托盘一片空白，紧接着又画 "?"（幽灵条目读不出值）。现在启动即
                # 画 "--"，有数据立刻换成数字（2026-10-13 用户：证明它启动了）。
                spec = I.IconSpec(None, False, ICON_SIZE, light_taskbar=light,
                                  no_devices=True)
                tip = "启动中…" if not self._ready else "未发现设备"
            else:
                # 有条目但全部读不出（关机充电的伪 None / 毛刺 0）：不再回落
                # 跟踪源（跟踪源那台未必在快照里，正是「托盘显示耳机、列表
                # 却没有」的裂缝），如实显示快照里第一台的 "?"。
                name0 = sorted(snap)[0]
                p0 = snap[name0]
                spec = I.IconSpec(p0, False, ICON_SIZE, light_taskbar=light)
                tip = self._tip_for(p0, False, name=name0)
            png = I.render_icon(spec)
            ok = self._set_icon(png, tip)
            if ok:                       # 只在真画上去后才更新缓存
                self._last_icon = spec
                self._last_tooltip = tip
            return ok
        except Exception as exc:  # noqa: BLE001
            log.warning("apply icon: %s", exc)
            return False

    def _on_sys_theme(self, dark: bool) -> None:
        """主题监听线程回调：任务栏换深浅后，按新配色重绘图标（警告态保留）。"""
        if self._last_icon is None:
            return
        try:
            # 走 _apply_icon 全流程：警告覆盖/配色/tooltip 一把重算
            self._apply_icon(*self._last_raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("theme icon redraw: %s", exc)

    def _on_device_change(self) -> None:
        """设备树变化回调（2026-10-13 事件驱动）：USB 插拔/设备开关机 → 立即重扫。

        由 `winevents` 去抖后触发（一次插拔的 10+ 条 NODES_CHANGED 合并成一次）。
        跑的仍是**常规刷新路径** `_refresh_from_tracked()`——不新增专用逻辑，
        保证事件驱动与轮询看到的是同一份判据（避免两套规则打架）。
        放在守护线程里跑：它内部会读硬件（蓝牙 CIM 查询等，可能几百 ms），
        不能阻塞消息循环线程（否则托盘菜单会卡）。线程内兜底异常，避免
        "USB 忙"这类瞬时错误冒到 threading 默认 hook 打脏日志。
        """
        def _run() -> None:
            try:
                self._refresh_from_tracked()
            except Exception as exc:  # noqa: BLE001
                log.debug("device-change refresh failed: %s", exc)
        threading.Thread(target=_run, daemon=True).start()

    def _on_taskbar_recreated(self) -> None:
        """任务栏重建回调（explorer 崩溃/重启）→ 重注册托盘图标。

        原理：explorer 重启后，旧任务栏连同我们注册的图标一起被销毁；不重发
        NIM_ADD 的话图标**永久消失**（用户只能手动重启 agent）。这里模拟一次
        "重新注册"：把 pystray 图标的 visible 关掉再打开 —— 这正是 §图标刷新
        链路里"内容变化走重新注册"用的同一招（`icon.visible = False/True`
        触发 NIM_DELETE + NIM_ADD）。
        """
        icon = self._icon
        if icon is None:
            return
        def _reid() -> None:
            try:
                icon.visible = False
                time.sleep(0.2)   # 给 shell 一点时间处理 NIM_DELETE
                icon.visible = True
                log.info("托盘图标已在新任务栏重建（NIM_ADD）")
            except Exception as exc:  # noqa: BLE001
                log.warning("taskbar icon rebuild failed: %s", exc)
        threading.Thread(target=_reid, daemon=True).start()

    def _on_mouse_activity(self) -> None:
        """鼠标活动回调（2026-10-13 RAWINPUT）→ **催一次罗技探测**，让条目尽快复活。

        ── 它解决什么 ──
        鼠标关机/休眠时条目会隐藏（判据 = HID++ 两次不应答），但**重新开机后
        要等下一轮 `_probe_mouse`**（~9s 轮询，退避还能到 30s）才复活。用户
        把鼠标晃两下却半天不亮 —— 这个回调把「鼠标动了」变成「立刻去问 HID++」，
        感知上就是秒回。

        ── 三条纪律（务必守住，否则会把响应当成误判）──
        1. **只当催化剂，不当判据**：RAWINPUT 拿不到「是哪只鼠标」，触摸板、
           别的鼠标、远程桌面都会进来。这里**只负责触发探测**，在线与否永远
           由 `_probe_mouse` 的 HID++ 应答决定。绝不因为"鼠标动了"就直接认为
           罗技在线。
        2. **只在「隐藏中或读数过期」时才催**：条目本来就新鲜时催探测纯属浪费
           （每次探测都读硬件）。用与 `_probe_mouse` 相同的 `MOUSE_FRESH` 门控。
        3. **不碰退避**：`_sleep_until` 是防狂戳硬件的。这里**不清退避**，
           只是"提前到点"——探测本身仍受退避约束，避免用户狂晃鼠标把硬件打爆。
        """
        now = time.monotonic()
        # 条目新鲜、或正处在失败退避期 → 不必催（探测也读不出新东西）
        if now < self._sleep_until:
            return
        if (self._last_info is not None
                and now - self._last_read_ts <= MOUSE_FRESH):
            return

        def _run() -> None:
            try:
                self._probe_mouse()
            except Exception as exc:  # noqa: BLE001 - 探测失败不该冒到线程 hook
                log.debug("mouse-activity probe failed: %s", exc)
        threading.Thread(target=_run, daemon=True).start()

    # ---------- 全屏/游戏感知（2026-10-13）----------
    def _in_dont_disturb(self) -> bool:
        """此刻是否"别打扰"（全屏/游戏/演示/安静时间）。

        给两个地方用：① 低电量 toast 前先问一句（`_maybe_notify_gate`）；
        ② 各轮询循环决定这一轮要不要真读硬件。**读不到 QUNS 时返回 False**
        （照常工作）—— 绝不因一次 API 抖动就把功能静音。
        """
        gate = getattr(self, "_fs_gate", None)
        return bool(gate is not None and gate.active)

    def _fs_poll_loop(self) -> None:
        """常驻循环：每 `POLL_SECONDS` 采一次 QUNS，维护迟滞门（守护线程）。

        ── 为什么用独立线程而不是挂在既有循环里 ──
        既有循环（`_bt_warm_loop` ~9s、`poll24` 60s）节拍都太粗：迟滞要求
        "进得快"（3 次），9s 一轮要 27s 才进静音 —— 游戏都打完一半了。这里
        用 2s 细节拍，代价仅一次 <0.1ms 的只读 API 调用。

        ── 为什么要迟滞 ──
        QUNS 随焦点抖动（点桌面、弹 UAC 都可能瞬时变）。见 `fullscreen.FullscreenGate`。
        """
        # 锁死模式（BATBAR_FULLSCREEN=on/off）只报一次日志，之后不必空转
        gate = self._fs_gate
        if gate.locked:
            log.info("全屏感知：BATBAR_FULLSCREEN=%s（锁死 active=%s，不再查 QUNS）",
                     gate.mode, gate.active)
        else:
            log.info("全屏感知已启动（每 %.0fs 采一次 QUNS，mode=%s）",
                     FS.POLL_SECONDS, gate.mode)
        while self._running:
            if not gate.locked:
                state = _query_quns()
                if gate.sample(state):
                    # 只在翻面那一刻打日志（否则 2s 一条刷屏）
                    if gate.active:
                        log.info("进入全屏/游戏态（%s）→ 静音提醒 + 暂停后台轮询",
                                 FS.state_name(state))
                    else:
                        log.info("退出全屏/游戏态（%s）→ 恢复提醒与正常轮询",
                                 FS.state_name(state))
            for _ in range(int(max(1, FS.POLL_SECONDS * 10))):
                if not self._running:
                    return
                time.sleep(0.1)

    def _gate_notify(self, name: str, pct: int) -> bool:
        """全屏门控的提醒开关：True = 允许弹。

        被抑制时**只计数不弹**（`_fs_skip_toast`）—— 关键取舍：全屏时不弹，
        但**不补弹**。理由是补弹会在游戏退出的瞬间堆一屏通知，比不弹更烦；
        且低电量本身是「慢变量」，用户退出后下一次 `feed` 周期（~9s）仍会
        正常触发提醒（届时已不在全屏态）。
        """
        if self._in_dont_disturb():
            self._fs_skip_toast += 1
            log.info("全屏/游戏中，抑制低电量提醒：%s = %d%%（累计抑制 %d 次）",
                     name, pct, self._fs_skip_toast)
            return False
        return True

    def _broadcast(self, data: bytes) -> None:
        with self._clients_lock:
            dead = []
            for c in self._clients:
                try:
                    c.sendall(data)
                except OSError:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)

    # ---------- 托盘 ----------
    def _key_for_name(self, name: str) -> str:
        """显示名 → 跟踪 key（菜单点击用）：hidpp / 24G:<基名> / BT:<名>。

        2.4G 与有线条目都走 `24G:` —— 之前误用 `BT:<名>` 去查蓝牙 CIM 列表，
        永远查不到 → 托盘图标画 "?"（2026-10-12 实测）。
        """
        from . import receiver24 as R
        if name == "鼠标 罗技 G304":
            return "hidpp"
        if name.endswith(R.SUFFIX) or name.endswith(R.WIRED_SUFFIX):
            return "24G:" + R.base_name(name)
        return f"BT:{name}"

    def _icon_display_name(self, snap: dict | None = None) -> str:
        """图标**此刻显示的是哪台**（菜单对勾必须与图标一致，用户需求 2026-10-12）。

        旧 bridge 模式（有外部连接）例外：那种模式图标由外部按
        跟踪源绘制，agent 侧的自绘选源不生效，对勾就还跟跟踪源走。
        `snap`：菜单构建传入的同一份快照（见 `_icon_target` 的说明）。
        """
        if self._clients:
            return self._tracked_name()
        try:
            tgt = self._icon_target(snap)
        except Exception:  # noqa: BLE001
            tgt = None
        return tgt[0] if tgt else self._tracked_name()

    def _build_tray(self):
        import pystray
        from PIL import Image

        def on_quit(icon, _item):
            self._running = False
            self._broadcast(wire.encode_msg("bye"))
            icon.stop()

        def on_refresh(_icon, _item):
            threading.Thread(target=self._manual_refresh, daemon=True).start()

        def on_open_log(_icon, _item):
            # 静默启动（无控制台）时这是查看运行状况的唯一入口
            try:
                import os
                from . import applog
                p = applog.current_path()
                if p is not None and p.exists():
                    os.startfile(str(p))
            except Exception as exc:      # noqa: BLE001 - 打不开日志不影响托盘
                log.debug("open log failed: %s", exc)

        def _make_switch(key: str, snap: dict):
            # 不能用 lambda _i, k=key：pystray 以 (icon, item) 两个位置参调用
            # 回调，第二个实参会覆盖 k 的默认值，把 MenuItem 对象当设备名传进
            # json.dumps → TypeError not JSON serializable。闭包工厂才稳。
            # snap = 弹菜单那一刻的同一份快照：点下去后图标刷新直接吃它，
            # 不再异步重读（旧实现 22:32 修「勾了 100 的 LEOBOG，几秒后图标
            # 显示 99」——菜单行是旧快照、点击又读新快照，ADC ±2 抖动跨帧）。
            def cb(_icon, _item):
                self._set_tracked(key, snap=snap)
            return cb

        def _static_items():
            """启动阶段（还没拿到任何真读数）的菜单：**绝不读硬件**。

            ⚠️ 这是「双击后 5~10 秒才看到托盘图标」的真凶（2026-10-13）：
            pystray `Icon.run()` 的顺序是 `_create_window()` → `_mark_ready()`
            → **`update_menu()`** →（队列通知 setup 线程）→ `visible = True`
            → `_show()`（NIM_ADD）。也就是说**菜单构建同步发生在图标注册之前**，
            而我们的菜单回调会走 `devices_snapshot()`（蓝牙 CIM 查询 4~25s +
            USB 枚举）—— 图标被整条菜单链卡在后面，等它注册完，后台早就把
            数字画好了，所以「图标一出现就是数字」，`--` 首帧根本来不及被看见。
            启动阶段返回这份静态清单（毫秒级）；托盘注册（`_tray_shown`）且拿到
            真读数（`_ready`）之后，`_install_menu_refresh` 装的钩子会在**每次
            弹出之前**重建菜单，那时这里才走下面的真实清单。
            """
            items = [
                pystray.MenuItem("启动中…", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("立即刷新", on_refresh, default=True),
            ]
            try:
                from . import applog
                if applog.current_path() is not None:
                    items.append(pystray.MenuItem("打开日志", on_open_log))
            except Exception:              # noqa: BLE001
                pass
            items.append(pystray.MenuItem("退出", on_quit))
            return items

        def _menu_items():
            # ★ 启动阶段：静态菜单，不读硬件（理由见 _static_items）。
            # ⚠️ 除 `_ready` 外还要看 `_tray_shown`（2026-10-13 10:25）：pystray
            # 的 `run()` 是「建窗口 → 建菜单 → 才 setup(visible=True)」，所以
            # **首次建菜单发生在 NIM_ADD 之前**。若那时 `_ready` 已置位（等待首帧
            # 会把它提前），这里就会去读一遍硬件（实测 ~3s）→ NIM_ADD 被拖到
            # +3.6s → 用户看到「四五秒后才出现电量数字」。托盘没注册之前一律
            # 用静态清单（毫秒级）。
            if not self._ready or not self._tray_shown:
                return _static_items()
            # pystray 每次弹出菜单都会重建（win32 后端 _create_menu），
            # 所以这里返回的清单永远是最新电量。文本是显示值，点击按索引切源。
            # 兜底：devices_snapshot 会起 PowerShell 子进程，偶发超时/异常若
            # 冒到 pystray 的建菜单循环（_win32.py 裸 except），整个 HMENU
            # 会烂在半路——宁可退化成静态项也不能让菜单崩。
            try:
                snap = self.devices_snapshot()
            except Exception as exc:  # noqa: BLE001
                log.warning("菜单快照失败：%s", exc)
                snap = {"鼠标 罗技 G304": self._last_info.percent if self._last_info else None}
            # 诊断（2026-10-13）：右键菜单是「图标有、列表没有」这类问题**唯一
            # 的现场** —— 记一份快照进日志，才能分清是「快照真空」还是「菜单
            # 读了另一份」。只在用户点菜单时打一行，不刷屏。
            log.info("菜单快照：%s", snap or "（空）")
            # 显示优先级（用户决策 2026-10-12）：耳机 > 鼠标 > 键盘。
            # 耳机用排除法判 —— 名字五花八门（WH-1000XM4 / AirPods…），没法规律。
            def _prio(name: str) -> int:
                n = name.lower()
                if "键盘" in name or "keyboard" in n:
                    return 2
                if "鼠标" in name or "mouse" in n:
                    return 1
                return 0
            items = []
            # 对勾 = 图标此刻显示的那台（用户需求 2026-10-12：对勾跟托盘要匹配）。
            # 点击某台 → 图标切到它并**粘住**（默认不回退，2026-10-12 22:32 用户
            # 拍板「勾了 LEOBOG 十几秒后自己跳回耳机」不合理）；只有它从快照
            # 消失才临时回到「低电长显 / 最低耳机」的自动选源，对勾跟着回去。
            # ⚠️ 必须复用上面这份 snap（2026-10-12 修「托盘 91 对勾行 92」）：
            #    再读一次 devices_snapshot 会跨到新的 2.4G 电量帧（ADC ±2 抖动），
            #    标签与对勾/图标就劈成两个值。
            try:
                icon_name = self._icon_display_name(snap)
                shown_key = self._key_for_name(icon_name)
            except Exception:  # noqa: BLE001 - 选源失败退化为跟踪源
                icon_name = self._tracked_name()
                shown_key = self._tracked
            # 打开菜单顺手把图标数字对齐到这份快照（对勾那台的读数）。
            # 图标平时只在轮询/帧回调时重绘，可能落后一帧；弹菜单这个动作
            # 说明用户正盯着看，此刻必须一致。
            try:
                if (self._last_icon is None
                        or self._last_icon.percent != snap.get(icon_name)):
                    self._apply_icon(snap.get(icon_name), False, snap)
            except Exception as exc:  # noqa: BLE001
                log.debug("menu icon sync: %s", exc)
            for name, pct in sorted(snap.items(), key=lambda kv: (_prio(kv[0]), kv[0])):
                key = self._key_for_name(name)
                # 读不出电量也用 "--"（2026-10-13 用户定稿：任何时候不显示问号）
                label = f"{name} {pct if pct is not None else '--'}%"
                if self._is_charging(name):        # 插线充电中 → 加 ⚡（有线/2.4G/蓝牙都适用）
                    label += " ⚡"
                items.append(pystray.MenuItem(
                    label, _make_switch(key, snap),
                    checked=(lambda _item, v=(key == shown_key): v)))
            if not items:
                items = [pystray.MenuItem("未发现设备", None, enabled=False)]
            items += [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("立即刷新", on_refresh, default=True),
            ]
            # 只有日志确实落盘了才给入口，否则点了没反应更困惑
            try:
                from . import applog
                if applog.current_path() is not None:
                    items.append(pystray.MenuItem("打开日志", on_open_log))
            except Exception:              # noqa: BLE001
                pass
            items.append(pystray.MenuItem("退出", on_quit))
            return items

        # pystray Menu 支持「单个 callable → 返回 items」：每次弹出重新求值，
        # 菜单里的电量数字与勾选态永远是最新的
        menu = pystray.Menu(_menu_items)
        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        self._icon = pystray.Icon("batbar", _IcoBmpImage(img), "Logitech 电量：启动中…", menu)
        # 菜单在**每次弹出之前**重建（pystray 默认复用旧 HMENU，见钩子里的说明）
        _install_menu_refresh(self._icon)
        # 建托盘这一步**同步**画一个 "--"（用户 00:31 需求：启动后立刻有图标，
        # 证明它起来了）。旧实现这里建的是全透明空图，真正的第一帧要等后台线程
        # 读完蓝牙（首次 CIM 查询实测 4~25s）→ 头几秒托盘区域一片空白，之后又
        # 因为「幽灵条目读不出值」先画 "?" 。现在这枪不读硬件、不等查询，立刻出图。
        self._paint_boot()
        _boot("托盘首帧已请求")
        # 托盘一建好就立刻按选源规则画一次：后台轮询先于本函数启动，它那一轮
        # 的绘制会因为「图标还没建」而落空（2026-10-12 实测：启动后图标一直
        # 停在初始空白/旧值）。这里补一枪，启动即显示正确设备。
        threading.Thread(target=self._refresh_from_tracked, daemon=True).start()

    def _wait_first_paint(self, timeout: float) -> float:
        """等首帧内容就绪（`_ready` 置位），最多 `timeout` 秒；返回实际等待秒数。

        只给启动用：让 NIM_ADD 那一帧就带上真读数（原因见 `run()` 里的长注释）。
        后台线程（`_build_tray` 起的那枪 + `_standalone_loop` / `_bt_warm_loop`）
        会在拿到读数后置位 `_ready`，所以这里纯等、不自己读硬件。
        """
        t0 = time.monotonic()
        while not self._ready and time.monotonic() - t0 < timeout:
            time.sleep(0.05)
        return time.monotonic() - t0

    def _paint_boot(self) -> bool:
        """启动占位图标：同步画 "--" + tooltip「启动中…」，不读任何硬件。

        与 `_apply_icon` 的区别：那条要读一遍快照（首次蓝牙 CIM 查询 4~25s），
        会把托盘首帧拖成几秒空白；这条是把「托盘已起来」这件事立刻画给用户看
        （2026-10-13 用户需求）。
        """
        from . import icons as I
        try:
            spec = I.IconSpec(None, False, ICON_SIZE,
                              light_taskbar=not _detect_sys_dark(), no_devices=True)
            ok = self._set_icon(I.render_icon(spec), "启动中…")
            if ok:
                self._last_icon, self._last_tooltip = spec, "启动中…"
            return ok
        except Exception as exc:      # noqa: BLE001 - 首帧画不出就等后台那一枪
            log.debug("boot icon: %s", exc)
            return False

    def _push_fresh_read(self):
        """托盘菜单"立即刷新"：不等下一轮轮询，直接读一次硬件并换图。

        两次都不应答 = 不在（2026-10-13 与 `_probe_mouse` 同口径，判据说明见
        `_read_with_second_chance`）：鼠标关机时点刷新，条目必须消失，而不是
        冻在最后一次读数上。⚠️ 11:00 用户实测纠正：旧实现把「真槽位不应答」
        当成睡眠保留条目，于是**关机后点刷新鼠标还在**（会话是鼠标开着时建的、
        一直挂在槽位 1 上）。

        ⚠️ 复活路径（2026-10-12 修「接收器插回后刷新多次都不出现」）：仅仅
        `_drop_device()` 只是把门打开，**不重建等于没修**——用户点十次刷新
        也只会看到十次「丢掉一个死会话」。所以 0xFF 回落会话丢完必须**当场
        重建并重读**：重建走 `_discover_devnum` 唤醒预算（第一枪唤醒、第二枪
        命中），加上刚才那一轮探测已经把鼠标敲醒过，这次大概率直接扫中槽位。
        同时清零 `_sleep_until`，别让后台探测还卡在 30s 退避里。"""
        try:
            sess = self._ensure_device()
            if sess is None:
                self._last_info = None
                return
            info = self._read_with_second_chance(sess)
            if info is None and sess._devnum == H.DEV_RECEIVER:
                # 0xFF 回落会话 = 接收器本体（扫不到对码设备），永远读不出。
                # 丢掉后立刻重建一次，本轮就给复活机会。
                self._drop_device()
                self._sleep_until = 0.0
                sess = self._ensure_device()
                info = (self._read_with_second_chance(sess)
                        if sess is not None else None)
            if info is not None:
                self._last_info, self._last_read_ts = info, time.monotonic()
                self._sleep_until = 0.0
                self._broadcast(wire.encode_msg("battery", source=info.source,
                                                percent=info.percent,
                                                charging=info.charging, state="ok"))
            else:
                if self._last_info is not None:
                    log.info("罗技条目隐藏：立即刷新两次读取都无应答（鼠标关机）")
                self._last_info = None
        except TransportClosed:
            self._drop_device()
            self._last_info = None
            self._sleep_until = 0.0

    def _manual_refresh(self) -> None:
        """菜单「立即刷新」：把**所有**数据源各推一把，不只 HID++ 鼠标。

        bug 13（2026-10-13 用户需求：点刷新要**立刻**看到关机的设备消失、
        托盘切到在线设备，而不是三四秒后）：三路数据源并行推进，每路出
        结论就重绘一次——
          ① 鼠标（HID++ 直读 ~0.5s）：两次不应答立即摘；
          ② 蓝牙：先 WinRT 连接态**双向快判**（`sync_now`，~0.6s，
             classic ∪ le 同进程）——一次查询同时摘掉确认离线的、加上确认
             上线的（bug 15），重绘；再 CIM **单枪**快查刷新电量值/发现
             新设备（quick=True，后台收尾）；
          ③ 2.4G：当场扫描（~1.5s/设备），应答制判在线。
        产品决策不变（2026-10-12）：2.4G 休眠与关机同权——扫描判不在，
        条目隐藏。"""
        self._push_fresh_read()
        self._repaint()                       # 鼠标结论先画上（~0.5s 内）

        def _bt_step() -> None:
            try:
                # 双向快判（bug 15）：一次 WinRT 查询同时摘离线 + 加上线，
                # 对称且不退化（分开调会各 spawn 一次 ~0.6s，下线时延翻倍）。
                self._bt_battery().sync_now()
            except Exception as exc:          # noqa: BLE001
                log.debug("manual refresh bt sync failed: %s", exc)
            self._repaint()                   # 加/摘完立刻画（~0.6s 处）
            try:
                self._bt_battery().force_refresh(quick=True)
            except Exception as exc:          # noqa: BLE001
                log.debug("manual refresh bt refresh failed: %s", exc)
            self._repaint()                   # 电量值/新设备刷新完再画

        def _g24_step() -> None:
            try:
                from . import poll24
                poll24.scan_now()             # 2.4G：当场扫一遍拿最新结论
            except Exception as exc:          # noqa: BLE001
                log.debug("manual refresh 24g scan failed: %s", exc)
            self._repaint()

        t_bt = threading.Thread(target=_bt_step, daemon=True)
        t_g24 = threading.Thread(target=_g24_step, daemon=True)
        t_bt.start()
        t_g24.start()
        t_bt.join()
        t_g24.join()
        self._repaint()                       # 全路收尾，保证最终一致

    def _repaint(self) -> None:
        """按当前缓存全量重算图标+重选源（不读硬件，~ms 级）。

        `_apply_icon` 内部 `_set_icon` 有锁，多线程并发重绘安全；pct/chg
        参数不参与选源（图标值/名字全部由 `_icon_target(snap)` 重算），
        传 None 即可。手动刷新每步完成后调它，让「摘一个、画一个」即时
        反馈到托盘，而不是等全部查询结束才换图（bug 13）。"""
        try:
            self._apply_icon(None, False)
        except Exception as exc:              # noqa: BLE001
            log.debug("repaint: %s", exc)

    def _set_icon(self, png: bytes, tooltip: str) -> bool:
        """图标+提示一起更新，返回是否真的应用成功。提示走原生 szTip：Win11 的
        托盘 tooltip 由 explorer（shell）绘制，天然就是音量/电池那种深色卡片 +
        微软雅黑 12px，与系统图标完全同款——无需（也不应）自绘。

        ⚠️ 必须**串行化**（2026-10-13 用户实测 `seticon failed: [WinError
        1402] 无效的光标句柄`）：图标更新有 4 个来源线程（`_bt_warm_loop`
        每轮、`_refresh_from_tracked`、dongle 帧回调、`_manual_refresh`），
        pystray 的 `icon` setter 内部是「先 `_release_icon()` 销毁旧句柄，
        再 `_assert_icon_handle()` 建新句柄」——两个线程交叉就会一个刚把
        句柄销毁、另一个拿着它去 NIM_MODIFY → 1402。更新失败时图标会**保留
        上一次的数字**，于是出现「托盘有电量、菜单却未发现设备」的劈叉。
        用 RLock（同线程可重入：`_menu_items` → `_apply_icon` → `_set_icon`
        这条链会重入）。
        """
        from contextlib import nullcontext

        from PIL import Image
        if not self._icon:
            return False          # 托盘还没建好：调用方要据此重试，别写缓存
        # getattr 兜底：测试里用 object.__new__ 造的 Agent 没有这把锁
        lock = getattr(self, "_icon_lock", None) or nullcontext()
        try:
            with lock:
                changed = ((png, tooltip) != getattr(self, "_last_painted", None))
                img = Image.open(io.BytesIO(png))
                self._icon.icon = _IcoBmpImage(img)   # pystray 热替换图标
                self._icon.title = tooltip or "Logitech"
                # ★ 内容变了要**重新注册**一次（NIM_DELETE + NIM_ADD），不能只靠
                #   `icon = ...` 那条 NIM_MODIFY：2026-10-13 实测（probes/
                #   probe_tray_modify.py + 日志时间线）——图标 add 之后的**第一次**
                #   NIM_MODIFY 会被 shell 收下（返回 1、GetLastError=0）却**不重绘**，
                #   托盘画面停在 add 那一帧（用户看到的 `--` + 「启动中…」），要等
                #   下一次刷新才换（实测 ~6s，正好是 `_bt_warm_loop` 那轮）——这就是
                #   「agent 日志 +0.6s 就画好了、用户 6~7s 才看到电量」的全部原因。
                #   而 **add 本身必然被渲染**（首帧 `--` 秒出就是证据），所以让每次
                #   **内容变化**都走一次 add。try/finally 保证异常时图标一定回到可见态。
                #   内容没变（如每 ~36s 的定期自愈重绘）仍走普通 modify，行为不变。
                if changed and getattr(self._icon, "visible", False):
                    try:
                        self._icon.visible = False
                    finally:
                        self._icon.visible = True
                if changed:
                    self._last_painted = (png, tooltip)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("seticon failed: %s", exc)
            return False

    # ---------- TCP（旧 bridge 模式，已非主路径） ----------
    def _serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((HOST, PORT))
        except OSError as exc:
            log.error("无法监听 %s:%d —— 端口被占或权限不足：%s", HOST, PORT, exc)
            return
        srv.listen(4)
        log.info("agent listening on %s:%d", HOST, PORT)
        while self._running:
            try:
                c, addr = srv.accept()
            except OSError:
                break
            log.info("controller connected from %s", addr[0])
            with self._clients_lock:
                self._clients.append(c)
            threading.Thread(target=self._handle_client, args=(c,), daemon=True).start()

    def _iter_lines(self, sock: socket.socket):
        buf = b""
        while self._running:
            try:
                data = sock.recv(4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line

    def _handle_client(self, c: socket.socket) -> None:
        try:
            for line in self._iter_lines(c):
                f = wire.decode_line(line)
                if f is None:
                    continue
                if f.type == "hello":
                    c.sendall(wire.encode_msg("hi", platform="win", hid=True))
                    c.sendall(wire.encode_msg("devices", **self.devices_snapshot()))
                elif f.type == "read":
                    self._do_read(c)
                elif f.type == "switch":
                    self._set_tracked(f.get("device", "hidpp"))
                    c.sendall(wire.encode_msg("devices", **self.devices_snapshot()))
                elif f.type == "icon":
                    self._set_icon(wire.un_b64_png(f.get("png", "")), f.get("tooltip", "Logitech"))
                elif f.type == "quit":
                    self._running = False
        except OSError:
            pass
        finally:
            with self._clients_lock:
                if c in self._clients:
                    self._clients.remove(c)
            try:
                c.close()
            except OSError:
                pass

    def _do_read(self, c: socket.socket) -> None:
        # 非 HIDPP 源（或 HIDPP 但缓存过期）统一走 _tracked_read；
        # hidpp 的 TTL 快速路径在 _tracked_read 内部实现
        pct, chg, st = self._tracked_read()
        source = "hidpp" if self._tracked == "hidpp" else self._tracked
        c.sendall(wire.encode_msg("battery", source=source, percent=pct,
                                  charging=chg, state=st))

    # ---------- standalone 模式 ----------
    def _standalone_loop(self) -> None:
        """本机模式：BatteryPoller 控节奏（60s 活跃 / 5min 睡眠退避），
        读数统一走 _tracked_read —— 菜单切换跟踪源（鼠标/键盘/耳机）后
        图标自动跟随。设备拔出也不崩：adapter 自愈 + 退避。

        ⚠️ 绘制必须走 `_apply_icon`（单一来源），不许在本函数里自己拼
        tooltip —— 2026-10-13 用户报「触摸板移到减号上显示 鼠标 罗技 G304:
        未知」，就是这里自己调 `_tip_for(pct)` 且没传 name、回落成跟踪源名。
        """
        from .state import BatteryPoller

        class _Adapter:
            def __init__(self, agent): self.a = agent
            def read_battery(self):
                pct, chg, st = self.a._tracked_read()
                if st == "error":
                    raise RuntimeError("tracked read error")
                if pct is None:
                    return None
                return H.BatteryInfo(pct, None, chg, None,
                                     "hidpp" if self.a._tracked == "hidpp" else self.a._tracked)
            def ping(self):
                # 睡眠判定：_tracked_read 的 sleeping 态即"对端不应"
                _pct, _chg, st = self.a._tracked_read()
                return None if st == "sleeping" else 1.0

        poller = BatteryPoller(session=_Adapter(self))
        while self._running:
            if poller.due():
                try:
                    poller.poll_once()
                except Exception as exc:  # noqa: BLE001 - adapter 已处理失效，这里兜底
                    log.debug("standalone poll: %s", exc)
            st = poller.take_icon_update()
            if st is not None:
                # ★ 与 `_apply_icon` 走**同一条**绘制路径（2026-10-13 修用户
                # 01:44 报「触摸板移到减号上显示 鼠标 罗技 G304: 未知」）：
                # 旧实现这里自己拼 `_tip_for(st.percent, charging)` —— **不传
                # name**，于是 `_tip_for` 回落到跟踪源名（"鼠标 罗技 G304"），
                # 一台设备都没连时托盘提示却写着这个不存在的鼠标名。而这条
                # 循环 0.5s 一次、比 `_bt_warm_loop`（~9s）密得多，所以它拼的
                # tooltip 会一直盖住 `_apply_icon` 写的那份「启动中…」。
                # 现在统一交给 `_apply_icon`：快照里没有任何真读数 → tooltip
                # 只说「启动中…」/「未发现设备」，绝不冒设备名；选源规则
                # （低电长显 / 最低耳机 / 兜底）也跟着统一。
                self._apply_icon(st.percent, bool(st.info and st.info.charging))
            time.sleep(0.5)

    def _bt_warm_loop(self) -> None:
        """常驻预热循环：把蓝牙设备列表的刷新挪到后台。

        CIM 查询实测约 4s（每节点最多三枪电量重试），而右键菜单是同步建的——
        不预热的话，用户点开菜单的那 4 秒会感觉到明显卡顿。这里定期焐热
        缓存，菜单侧只吃旧值即可（电量秒级不变，旧几十秒无感）。
        """
        while self._running:
            # ── 全屏/游戏态：跳过本轮的硬件读取与重绘（2026-10-13）──
            # 游戏已吃满 CPU/GPU，agent 再周期性读 HID++、跑 CIM 查询只会
            # 雪上加霜；何况此刻用户根本不看托盘。只睡、不干活，等退出全屏
            # 自然恢复（`_fs_gate` 的迟滞保证不抖动）。
            # 注意：**不重置 `self._ready`** —— 退出全屏后第一轮照常
            # `_boot("首轮采集完成")` 的判断只在启动期成立，这里跳过不影响。
            if self._in_dont_disturb():
                for _ in range(10):
                    if not self._running:
                        return
                    time.sleep(0.5)
                continue
            try:
                self._bt_battery().prewarm()
            except Exception:  # noqa: BLE001 - 预热失败不影响任何功能
                log.debug("bt prewarm: %s", "failed")
            # 罗技条目在/离线判定（2026-10-12 参考 OpenLogi）：读数过期就探一次，
            # 不应答 = 鼠标关机/接收器拔掉 → 条目隐藏。新鲜度与退避在函数内自判。
            try:
                self._probe_mouse()
            except Exception as exc:  # noqa: BLE001
                log.debug("mouse probe loop: %s", exc)
            # 列表焐热后顺手喂一遍低电量状态机（覆盖全部设备，不限跟踪源）。
            # prewarm 实测 ~4s + 5s 节拍 ≈ 每 ~9s 一轮，天然满足去抖节奏。
            self._feed_snapshot()
            # 第一轮采集跑完 = 「已就绪」：此后快照仍为空就如实说「未发现设备」，
            # 不再把「启动中…」一直挂下去（2026-10-13）。有读数时 _apply_icon
            # 也会置位，两条互为兜底。置位同时把启动静态菜单换成真实清单。
            was_boot = not self._ready
            if was_boot:
                _boot("首轮采集完成")
            self._ready = True
            # 图标选源：手动窗口 > 低电长显 > 最低耳机（变化才重绘）。
            # ⚠️ 只有**真的画上去**才写缓存：本循环先于 _build_tray 启动，
            #    托盘未建好时 _set_icon 是空操作，若照样写缓存，图标会永远
            #    停在初始值（2026-10-12 实测 bug：启动后一直显示 90）。
            try:
                tgt = self._icon_target()
                self._ticks += 1
                # 自愈判据：图标已建但画的内容和目标不符（含「一次都没画过」）
                stale = False
                if self._icon is not None:
                    if tgt is None:
                        # ★ 选不出任何目标（快照空/全读不出）→ 图标也必须回到
                        # "--"，不许留着上一个设备的数字（2026-10-13：否则
                        # 「图标有、列表没有」会以另一种形式出现）。
                        stale = (self._last_icon is not None
                                 and self._last_icon.percent is not None)
                    elif self._last_icon is None or \
                            (self._last_icon.percent, self._last_icon.warning) \
                            != (tgt[1], tgt[2]):
                        stale = True
                # 每 ~36s 无条件重绘一次：部分第三方任务栏（Seelen UI 等）会
                # 缓存托盘图标、不响应 NIM_MODIFY，定期换新句柄可自愈。
                # ★ `was_boot`：首轮跑完必须**当轮**重绘（2026-10-13 09:38 用户
                # 报「只有一个鼠标时『启动中…』挂很久」）。空快照下 `stale`
                # 恒 False——启动占位图本来就是 `--`（percent=None），而 stale
                # 比的是**图标数字**；`tgt` 与 `_last_warn` 又都是 None ⇒ 只能
                # 等 `_ticks % 4 == 0` 那一次（每轮 ~5s，约 20s 后才换文案）。
                # 多设备时之所以「几乎看不到启动中」，是 poller 那条 0.5s 循环
                # 顺手重绘掩盖了这个缺口；只挂鼠标时那条循环没内容可推，缺口
                # 才暴露。「启动中…」→「未发现设备」是**文案**变化，必须显式带上。
                if was_boot or tgt != self._last_warn or stale or self._ticks % 4 == 0:
                    if self._refresh_from_tracked():
                        self._last_warn = tgt
            except Exception as exc:  # noqa: BLE001
                log.debug("warn icon refresh: %s", exc)
            # 每 5s 检查一次，真正刷新由 TTL(20s) 与 _refreshing 去重控制
            for _ in range(10):
                if not self._running:
                    return
                time.sleep(0.5)

    # ---------- 入口 ----------
    def run(self, standalone: bool = False) -> None:
        # ★ 托盘必须第一个建（2026-10-13 用户需求「启动后立刻看到托盘」）：
        # NIM_ADD 发得越早，explorer 越早把它画出来。旧顺序是把 3 个后台线程 +
        # dongle/feature24/poll24/wired24 全起来之后才建托盘，首帧白白排在它们
        # 后面（虽然各自只是毫秒级，但没理由让「证明我启动了」这件事排队）。
        self._build_tray()
        assert self._icon is not None
        if not standalone:
            threading.Thread(target=self._serve, daemon=True).start()
        else:
            threading.Thread(target=self._standalone_loop, daemon=True).start()
        threading.Thread(target=self._bt_warm_loop, daemon=True).start()
        # 全屏/游戏感知（2026-10-13）：独立线程每 2s 采一次 QUNS 维护迟滞门。
        # 单独起线程是因为既有循环节拍太粗（~9s/60s），迟滞会慢到游戏打完；
        # 而这次查询是纯只读、<0.1ms，2s 节拍毫无压力。
        threading.Thread(target=self._fs_poll_loop, name="fs-watch",
                         daemon=True).start()
        # 2.4G 接收器厂商通道的被动监听（只读，不往设备写任何字节）：
        # 拿到键盘在 dongle 模式下的实时电量；休眠帧/静默超时 → 条目自动消失。
        try:
            from . import dongle

            def _on_dongle_frame(name, kind, value):
                # 电量帧即时喂低电量状态机（feed 自带连续 confirm 去抖，孤立
                # 0 帧毛刺滤得掉；这里不能被下面的 UI 节流挡住，否则 2.4G
                # 低电要等下一次后台轮询才计数）。
                if kind == "battery":
                    self._feed_lowbat(name, value)
                # 帧到（电量刷新或休眠切换）→ 立即重算图标/菜单，不等轮询。
                # 电量帧可能连发，2s 节流防抖。
                now = time.monotonic()
                if now - self._last_dongle_refresh < 2.0:
                    return
                self._last_dongle_refresh = now
                self._refresh_from_tracked()

            dongle.on_frame(_on_dongle_frame)
            dongle.start()
        except Exception as exc:  # noqa: BLE001 - 监听失败不影响其它源
            log.debug("dongle listener unavailable: %s", exc)
        # Feature-report 通道（迈从 K87 电量藏 0xFF04 口的 feature 0x06，
        # 纯拉取式）：后台 30s 轮询刷新读数。与 dongle 监听不同 collection、
        # 同步 IOCTL 不抢输入队列，可并存。（旧「拉取成功转记活帧防误判离线」
        # 已删——读到的可能是接收器静态缓冲，不代表键盘活着；在/离线唯一
        # 判据改为 poll24 扫描。）
        try:
            from . import feature24
            feature24.start()
        except Exception as exc:  # noqa: BLE001 - feature 失败不影响其它源
            log.debug("feature24 unavailable: %s", exc)
        # 2.4G 扫描器（2026-10-12 扫描制）：启动扫一遍、每 5 分钟一遍、
        # 「立即刷新」再扫一遍。逐个目标代发官方 getBattery 查询帧
        # （13 4a 01…5e），本轮窗口内 frame_ts 有前进 = 在线，否则不在、
        # 条目隐藏。独立只写句柄，不碰监听线程的输入队列。
        try:
            from . import poll24
            # 全屏/游戏中跳过扫描（2026-10-13）：注入判据，poll24 保持模块独立
            poll24.set_skip_when(self._in_dont_disturb)
            poll24.start()
        except Exception as exc:  # noqa: BLE001 - poll 失败不影响其它源
            log.debug("poll24 unavailable: %s", exc)
        # 有线（USB 数据线）通道：键盘插线时读它自己的 520B feature 接口。
        # 覆盖「三档开关拨到关闭 + 插线」这种无线全死、只有有线能读电量的组合；
        # 也提供充电标志（图标 ⚡ + 低电豁免）。
        try:
            from . import wired24
            wired24.start()
        except Exception as exc:  # noqa: BLE001 - 有线失败不影响其它源
            log.debug("wired24 unavailable: %s", exc)
        _boot("后台通道就绪")

        # ★ 等首帧内容就绪再注册图标（2026-10-13 09:49 + 10:05 用户两次报「6~7 秒
        #   才显示电量」）：实测 shell 在图标 add 之后的一段时间里**收下 NIM_MODIFY
        #   却不重绘**（`probes/probe_tray_modify.py`：返回 1、GetLastError=0，托盘
        #   画面却停在 add 那一帧），而 **add 那一帧必然被渲染**（首帧 `--` 秒出即证）。
        #   所以把真读数赶在 add 之前准备好 —— 托盘一出现就是电量，而不是先挂
        #   `--` +「启动中…」。有界等待：鼠标在睡 / 一台设备都没有时，最多等
        #   BOOT_SHOW_WAIT 秒就照旧注册 `--`（用户 00:31 的「启动就显示两个减号」）。
        waited = self._wait_first_paint(BOOT_SHOW_WAIT)

        def _setup(icon):
            # pystray 默认的 setup 就是 `visible = True`；这里显式写一遍只为
            # 打一个点 —— 它标记「图标真正注册进托盘（NIM_ADD）」的时刻，
            # 是判断「慢在进程里还是慢在 explorer 显示」的分界线。
            # 2026-10-13 起同时记下**注册那一刻的内容**与等待时长：这样日志就能
            # 直接回答「add 那一帧画的是不是电量」。
            icon.visible = True
            self._tray_shown = True
            _boot("图标已注册（NIM_ADD）内容=%r 等待=%.2fs" % (icon.title, waited))
            # 菜单不再需要「一次性换清单」：`_install_menu_refresh` 装的钩子会在
            # **每次弹出之前**重建（那时 `_ready`/`_tray_shown` 都已就绪 → 真实
            # 清单）。启动这一次建菜单保持静态（毫秒级），不挡 NIM_ADD。

        # 主线程消息循环；菜单"退出"里置 _running=False
        self._icon.run(setup=_setup)


def _probe(a: Agent) -> int:
    """读一次电量打印，验证 HID++ 协议层对该设备是否生效。"""
    sess = a._ensure_device()
    if sess is None:
        print("未发现罗技 HID++ 设备：确认 Bolt/Unifying 接收器已插好、鼠标已开机（点一下鼠标唤醒）")
        return 2
    try:
        info = sess.read_battery()
    except TransportClosed:
        info = None
    if info is None:
        try:
            p = sess.ping()
        except TransportClosed:
            p = None
        print(f"读不到电量。ping={'通(协议OK,电池特性未命中——加 -v 看特性发现日志)' if p else '不通(设备休眠/未配对——动一下鼠标重试)'}")
        a._drop_device()
        return 3
    a._drop_device()
    print(f"电量 {info.percent}%  charging={info.charging}  特性={info.source}  电压={info.voltage_mv}  档位={info.approx}")
    return 0


def _raw_echo(dev, frame_len: int, siblings=(), extra_listeners=()) -> None:
    """原始帧回显：向 0xFF 和槽位 1-3 各发一发 ping，监听 1.5s 打印所有回帧。
    用于区分「设备完全不回」和「回了但我们的匹配/解析逻辑没认出来」。"""
    from . import transport as T
    print(f"  -- 原始回显（写长度={frame_len}，发 ping 监听 1.5s）--")
    mark = 0x5A
    reqs = {}
    for k, idx in enumerate((H.DEV_RECEIVER, 1, 2, 3)):
        # Solaar base.py:821 ping 帧 data = request_id(2B) + 0x00 + 0x00 + mark(1B) = 5B
        payload = struct.pack("!HBBB", 0x0018 | (8 + k), 0, 0, mark)
        try:
            T.try_write(dev, H.build_write_frame(idx, payload), sizes=(frame_len,))
            reqs[idx] = payload[:2]
        except Exception as exc:  # noqa: BLE001
            print(f"     idx={idx} 写失败：{exc}")
    # 回帧可能从同 usage_page 的**另一个** collection 出来（个别 Bolt 固件 TX/RX 分离），
    # 所以监听时把同 up 的其他可打开 collection 一起 poll。
    watchers = [w[1] for w in siblings if w[1] is not dev] + list(extra_listeners)
    got = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.5:
        raw = dev.read(64)
        src = "tx"
        if not raw:
            for wdev in watchers:
                try:
                    raw = wdev.read(64)
                except OSError:
                    continue
                if raw:
                    src = "rx"
                    break
        if not raw:
            time.sleep(0.02)
            continue
        b = bytes(raw)
        note = ""
        parsed = H.parse_input_report(b)
        if parsed:
            _rid, dn, pl = parsed
            for idx, head in reqs.items():
                if pl[:2] == head:
                    note = f"  ← 匹配 idx={idx} 回帧"
        print(f"     ({src}) [{b.hex(' ')}]{note}")
        got += 1
    if got == 0:
        print("     （1.5 秒内零回帧——接收器对 ping 无响应）")


def _scan(a: Agent) -> int:
    """诊断模式：枚举本机所有罗技 HID collection，逐个对接收器(0xFF)+槽位(1-7)
    做 ping 与电池特性探测，打印命中项。用于定位你的设备该走哪个 collection、
    哪个 device_index——接收器把鼠标藏在槽位 1-7，不是 0xFF。"""
    import hid as _hid
    from .session import HidppSession
    from . import transport as T

    print("== 本机所有 046D（罗技）HID collection ==")
    cands = []
    for d in _hid.enumerate():
        if d.get("vendor_id") != 0x046D:
            continue
        up, uu = d.get("usage_page"), d.get("usage")
        print(f"  up=0x{up:04X} uu=0x{uu:04X}  pid=0x{d['product_id']:04X}  "
              f"{d.get('product_string')!r}  serial={d.get('serial_number')!r}")
        cands.append(d)
    if not cands:
        print("  （没找到任何罗技 HID 设备，确认接收器已插好）")
        return 2

    print("\n== 写入自检（Windows 要求写长度精确匹配 output report 大小）==")
    writable = []
    listeners = []   # 写失败但保持打开：可能专职回帧（个别 Bolt 固件 TX/RX 分 collection）
    for d in cands:
        try:
            dev = T.open_path(d["path"])
        except Exception as exc:  # noqa: BLE001 - 某些 collection 被系统占用打不开
            print(f"  up=0x{d.get('usage_page'):04X} 打开失败：{exc}")
            continue
        # 拿一个真实形状的帧做试探：ROOT.ping(0xFF)，内容无所谓，只看写是否被接受
        test_frame = H.build_write_frame(H.DEV_RECEIVER, bytes([0x00, 0x10, 0x00]))
        n = T.try_write(dev, test_frame)
        if n is None:
            print(f"  up=0x{d.get('usage_page'):04X} uu=0x{d.get('usage'):04X} 写被拒（保留为只读监听）")
            listeners.append(dev)
            continue
        print(f"  up=0x{d.get('usage_page'):04X} uu=0x{d.get('usage'):04X} √ 可用写长度 = {n} 字节")
        writable.append((d, dev, n))

    print("\n== 逐 collection × 逐 device_index 探测（电池特性为主判据）==")
    print("   （实测：Bolt 对 ping 回错误帧 0x01——不作存活判据；设备长帧回复")
    print("     可能出现在同页另一个 collection，故读 = 全部已打开句柄）")
    hits = []
    all_devs = [dev for (_d, dev, _n) in writable] + list(listeners)
    for d, dev, n in writable:
        # 原始回显测试：向接收器发一发 ping，1.5s 内把所有回帧打出来。
        sibs = [w for w in writable if w[0].get("usage_page") == d.get("usage_page")]
        _raw_echo(dev, n, siblings=sibs, extra_listeners=listeners)
        readers = [x for x in all_devs if x is not dev]
        for idx in [H.DEV_RECEIVER, 1, 2, 3, 4, 5, 6, 7]:
            s = HidppSession(T.HidapiTransport(dev, frame_len=n, readers=readers),
                             device_index=idx, timeout=0.4, retries=0)
            try:
                # 主判据 = 电池特性（标准 feature call，可靠）
                bf = s.battery_feature()
                if bf is None:
                    continue
                info = s.read_battery()
                tag = (f"  up=0x{d.get('usage_page'):04X} idx={idx}  "
                       f"电池特性=0x{bf[0]:04X}@idx{bf[1]}  读数={info.percent if info else 'N/A'}%"
                       f"  charging={info.charging if info else '-'}")
                hits.append((d["path"], d.get("usage_page"), idx, bf))
                print(tag)
            except TransportClosed as exc:
                print(f"  up=0x{d.get('usage_page'):04X} idx={idx} 设备失效：{exc}")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  up=0x{d.get('usage_page'):04X} idx={idx} 异常：{type(exc).__name__}: {exc}")
    for x in all_devs:
        try:
            x.close()
        except Exception:  # noqa: BLE001
            pass

    print("\n== 结论 ==")
    if hits:
        path, up, idx, bf = hits[0]
        print(f"√ 可用组合：usage_page=0x{up:04X}, device_index={idx}, 电池特性=0x{bf[0]:04X}")
        print(f"   → 在 agent 里设 BATBAR_USAGE_PAGE=0x{up:04X} BATBAR_DEVNUM={idx} 即可锁定")
    else:
        print("× 没有任何槽位读到电池特性。可能：鼠标真在睡（多晃几下再跑）/ "
              "该接收器走的是标准 HID 电量而非 HID++（那要换读取路径）")
    return 0


def _enable_dpi_awareness() -> None:
    """声明进程 DPI 感知（必须在创建任何窗口/菜单之前调用）。

    本机（Win11 2K@缩放 + 火绒 SystrayComponent 托盘宿主）实测三态
    （2026-10-11 逐项 A/B + 截图验证）：
      不声明       -> 中文正常但原生菜单被位图拉伸**发虚**
      PMv2 (-4)   -> 清晰但宿主把每项中文塌成单个 å（宿主渲染 bug）
      SYSTEM (-2) -> **清晰且不塌字**，唯一双全档 → 默认

    环境变量 ``BATBAR_DPI``（大小写不敏感）：
      未设          -> system（默认）
      system / sys  -> PROCESS_SYSTEM_DPI_AWARE
      pm2 / 1       -> Per-Monitor V2，逐级回退（本机中文塌字，仅别处调试用）
      off / 0 / no  -> 完全不声明（发虚但绝不出渲染幺蛾子，排障兜底）
    """
    import os
    mode = (os.environ.get("BATBAR_DPI") or "system").strip().lower()
    if mode in ("0", "off", "no"):
        log.debug("DPI awareness off (BATBAR_DPI=%s)", mode)
        return
    import ctypes
    try:
        user32 = ctypes.windll.user32
    except AttributeError:      # 非 Windows（单测环境）
        return
    ok = False
    if mode in ("system", "sys"):
        try:  # Win10 1703+: DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
            ok = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-2)))
        except AttributeError:
            pass
        if not ok:
            try:  # Win8.1: PROCESS_SYSTEM_DPI_AWARE = 1
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
                ok = True
            except (AttributeError, OSError):
                pass
    else:  # pm2 / 1：Per-Monitor V2 逐级回退
        try:  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            ok = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
        except AttributeError:
            pass
        if not ok:
            try:  # Win8.1: PROCESS_PER_MONITOR_DPI_AWARE = 2
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
                ok = True
            except (AttributeError, OSError):
                try:  # Vista+: 系统级感知兜底
                    ok = bool(user32.SetProcessDPIAware())
                except Exception:  # noqa: BLE001
                    pass
    log.info("dpi awareness mode=%s enabled=%s", mode, ok)


# uxtheme 序号 API（_enable_dark_menus 成功时置位），供启动后主题切换复用
_set_preferred_app_mode = None
_flush_menu_themes = None
_theme_listeners: list = []   # 主题切换回调（Agent 注册图标重绘）
# 设备树变化回调（2026-10-13）：WM_DEVICECHANGE 去抖后触发，Agent 注册"立即刷新"。
# 与 _theme_listeners 分开，因为触发语义不同（主题=重绘图标；设备=重扫数据源）。
_device_listeners: list = []
# 任务栏重建回调（2026-10-13）：explorer 重启后 WM_TASKBARCREATED，Agent 重注册图标。
_taskbar_listeners: list = []
# 鼠标活动回调（2026-10-13 RAWINPUT）：WM_INPUT 去抖后触发，Agent 催一次 HID++ 探测。
# ⚠️ 与 _device_listeners 分开：语义完全不同 —— 设备事件=重扫全部数据源，
#    鼠标活动=只是「催化剂」，催罗技探测尽快复活（见 `_on_mouse_activity`）。
_mouse_listeners: list = []


def _detect_sys_dark() -> bool:
    """系统是否深色模式 —— 跟随设置页「选择你的默认 Windows 模式」。

    本机实测映射（2026-10-12，自定义混搭：Windows 模式=深色、应用模式=浅色）：
      「默认 Windows 模式」  → SystemUsesLightTheme   （深色=0）
      「默认应用模式」       → AppsUseLightTheme      （与本菜单无关）
    因此判据 = SystemUsesLightTheme == 0；该键缺失（老系统/未写过自定义）
    时回退 AppsUseLightTheme。注册表异常按浅色。"""
    import winreg
    try:
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
    except OSError:
        return False
    try:
        for name in ("SystemUsesLightTheme", "AppsUseLightTheme"):
            try:
                v, _ = winreg.QueryValueEx(k, name)
                return int(v) == 0
            except OSError:
                continue       # 该键不存在，试下一个
        return False
    finally:
        winreg.CloseKey(k)


def _apply_menu_dark(dark: bool) -> None:
    """按 dark 切换原生菜单深浅：dark→ForceDark(2)，否则 ForceLight(3)，
    前后各刷一次 FlushMenuThemes 让已缓存主题失效。
    序号 API 未就绪（Win<11 / 非 Windows）时静默跳过。"""
    if not _set_preferred_app_mode or not _flush_menu_themes:
        return
    try:
        _flush_menu_themes()
        _set_preferred_app_mode(2 if dark else 3)
        _flush_menu_themes()
        log.info("menu theme -> %s", "dark" if dark else "light")
    except Exception as exc:  # noqa: BLE001
        log.debug("apply menu dark(%s) failed: %s", dark, exc)


def _start_theme_watch() -> None:
    """守护线程：建隐藏消息窗口，监听颜色/主题变更广播，系统深浅色切换时
    实时重设菜单配色。注册表值只在变更信号后重读，避免轮询。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    WM_DESTROY = 2
    WM_DWMCOLORIZATIONCOLORCHANGED = 0x0320
    WM_SETTINGCHANGE = 0x001A
    WM_THEMECHANGED = 0x031A

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                 ctypes.c_uint, wintypes.WPARAM,
                                 wintypes.LPARAM)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]

    state = {"dark": _detect_sys_dark()}
    keeping = []   # 持 WNDPROC 强引用防 GC（回调悬垂会崩进程）

    # ── 设备事件去抖（2026-10-13）──
    # 实测一次插拔会连发 10+ 条 DBT_DEVNODES_CHANGED（见 winevents 模块头），
    # 必须合并成一次刷新。**不用线程 sleep 去抖**（会占线程、且和消息循环竞态），
    # 改用 Win32 `SetTimer`：每次事件来时重置一次性定时器，静默 DEBOUNCE_MS 后
    # 窗口收到 WM_TIMER，此时才真正触发刷新 —— 天然"这一波结束了"。
    from . import winevents as _we

    WM_TIMER = 0x0113
    WM_DEVICECHANGE = _we.WM_DEVICECHANGE
    WM_INPUT = _we.WM_INPUT
    WM_TASKBARCREATED = user32.RegisterWindowMessageW("TaskbarCreated")
    DEBOUNCE_TIMER_ID = 1
    DEBOUNCE_MS = int(_we.DEBOUNCE_SECONDS * 1000)
    # RAWINPUT 鼠标活动用**独立**定时器：它比设备事件密得多、去抖窗口也更短
    # （0.25s vs 0.6s），共用一个 ID 会把两种节奏搅在一起。
    MOUSE_TIMER_ID = 2
    MOUSE_MS = int(_we.RAWINPUT_DEBOUNCE_SECONDS * 1000)
    ev_state = {"fired": 0}

    def _on_device_event(kind: str) -> None:
        """设备事件已静默 → 通知所有订阅者（各自重扫自己的数据源）。"""
        ev_state["fired"] += 1
        log.info("设备树变化（%s）→ 触发即时刷新（第 %d 次）",
                 kind, ev_state["fired"])
        for cb in list(_device_listeners):
            try:
                cb()
            except Exception as exc:  # noqa: BLE001 - 单个订阅者失败不拖累其余
                log.debug("device listener failed: %s", exc)

    def _on_mouse_event() -> None:
        """RAWINPUT 已静默 → 通知订阅者「鼠标刚有活动」（催一次罗技探测）。"""
        # DEBUG 级心跳：排障时能一眼看出 RAWINPUT 是不是真在工作
        # （seen=收到多少条 WM_INPUT，active=其中判定为"真实活动"的条数）。
        log.debug("鼠标活动去抖到点（seen=%d active=%d）→ 催探测",
                  raw_stat["seen"], raw_stat["active"])
        for cb in list(_mouse_listeners):
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                log.debug("mouse listener failed: %s", exc)

    # GetRawInputData 两步法：先问大小（pData=NULL），再取数据。缓冲复用。
    # 签名（x64 必须显式声明，否则 HANDLE 被默认 c_int 截断）。
    user32.GetRawInputData.restype = wintypes.UINT
    user32.GetRawInputData.argtypes = (wintypes.HANDLE, wintypes.UINT,
                                       wintypes.LPVOID,
                                       ctypes.POINTER(wintypes.UINT),
                                       wintypes.UINT)
    RID_INPUT = 0x10000003
    raw_stat = {"seen": 0, "active": 0}

    def _wnd_proc(hwnd, msg, wparam, lparam):
        if msg in (WM_DWMCOLORIZATIONCOLORCHANGED, WM_SETTINGCHANGE,
                   WM_THEMECHANGED):
            new = _detect_sys_dark()
            if new != state["dark"]:
                state["dark"] = new
                _apply_menu_dark(new)
                for cb in list(_theme_listeners):   # 图标重绘等订阅者
                    try:
                        cb(new)
                    except Exception:  # noqa: BLE001
                        pass
        elif msg == WM_DEVICECHANGE:
            kind = _we.classify(wparam)
            if kind is not None:
                # 重置去抖定时器：一波连发只在最后一帧之后 DEBOUNCE_MS 触发一次
                user32.SetTimer(hwnd, DEBOUNCE_TIMER_ID, DEBOUNCE_MS, None)
        elif msg == WM_TIMER and wparam == DEBOUNCE_TIMER_ID:
            user32.KillTimer(hwnd, DEBOUNCE_TIMER_ID)
            _on_device_event("debounced")
        elif msg == WM_INPUT:
            # ── RAWINPUT 鼠标活动（2026-10-13）──
            # 只关心"有没有真实鼠标活动"：是 → 重置鼠标去抖定时器。
            # 解析失败/非鼠标（键盘/HID）→ 忽略。细节见 winevents 的 RAWINPUT 节。
            size = wintypes.UINT(0)
            n = user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT,
                                       None, ctypes.byref(size),
                                       ctypes.sizeof(wintypes.HANDLE) * 3)
            if n == 0 and size.value:
                buf = ctypes.create_string_buffer(size.value)
                cap = wintypes.UINT(size.value)
                n = user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT,
                                           buf, ctypes.byref(cap),
                                           ctypes.sizeof(wintypes.HANDLE) * 3)
                if n != 0xFFFFFFFF:
                    raw_stat["seen"] += 1
                    if _we.rawinput_activity(buf.raw[:n]):
                        raw_stat["active"] += 1
                        user32.SetTimer(hwnd, MOUSE_TIMER_ID, MOUSE_MS, None)
            # WM_INPUT 必须返回 0（DefWindowProc 会替我们调 RIDEV_REMOVE 清理）
            return 0
        elif msg == WM_TIMER and wparam == MOUSE_TIMER_ID:
            user32.KillTimer(hwnd, MOUSE_TIMER_ID)
            _on_mouse_event()
        elif msg == WM_TASKBARCREATED:
            # explorer 崩溃/重启后任务栏重建 —— 图标已随旧任务栏消失，必须重注册
            log.info("任务栏已重建（explorer 重启）→ 重建托盘图标")
            for cb in list(_taskbar_listeners):
                try:
                    cb()
                except Exception as exc:  # noqa: BLE001
                    log.debug("taskbar listener failed: %s", exc)
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(_wnd_proc)
    keeping.append(proc)

    def _loop():
        # 64 位下必须显式声明签名，否则 HWND 等句柄被默认 c_int 截断
        dw = user32.DefWindowProcW
        dw.restype = ctypes.c_ssize_t
        dw.argtypes = (wintypes.HWND, ctypes.c_uint,
                       wintypes.WPARAM, wintypes.LPARAM)
        user32.PostQuitMessage.argtypes = (ctypes.c_int,)
        gm = user32.GetMessageW
        gm.restype = ctypes.c_int
        gm.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                       ctypes.c_uint, ctypes.c_uint)
        user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        dm = user32.DispatchMessageW
        dm.restype = ctypes.c_ssize_t
        dm.argtypes = (ctypes.POINTER(wintypes.MSG),)
        rc = user32.RegisterClassW
        rc.restype = ctypes.c_ushort
        rc.argtypes = (ctypes.POINTER(WNDCLASS),)
        cw = user32.CreateWindowExW
        cw.restype = wintypes.HWND
        cw.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                       wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        hinst = kernel32.GetModuleHandleW(None)
        kernel32.GetModuleHandleW.restype = None
        # SetTimer/KillTimer 签名（WM_TIMER 去抖用）
        user32.SetTimer.restype = wintypes.UINT
        user32.SetTimer.argtypes = (wintypes.HWND, wintypes.UINT,
                                    wintypes.UINT, wintypes.LPVOID)
        user32.KillTimer.restype = wintypes.BOOL
        user32.KillTimer.argtypes = (wintypes.HWND, wintypes.UINT)
        wc = WNDCLASS(style=0, lpfnWndProc=proc, hInstance=hinst,
                      lpszClassName="BatbarThemeWatcher")
        if not user32.RegisterClassW(ctypes.byref(wc)):
            log.debug("theme watch: RegisterClass failed rc=%s",
                      ctypes.GetLastError())
            return
        hwnd = user32.CreateWindowExW(0, "BatbarThemeWatcher", "", 0,
                                      0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            log.debug("theme watch: CreateWindow failed rc=%s",
                      ctypes.GetLastError())
            return
        # ── 注册 USB 设备接口通知（拿到精确的 ARRIVAL/REMOVECOMPLETE）──
        # 实测：不注册也能收到 DBT_DEVNODES_CHANGED，但注册后能区分"插/拔"。
        # 失败不影响主流程（NODES_CHANGED 兜底）。
        try:
            _register_usb_notify(user32, hwnd)
        except Exception as exc:  # noqa: BLE001
            log.debug("theme watch: RegisterDeviceNotification 失败: %s", exc)
        # ── 注册 RAWINPUT 鼠标监听（2026-10-13）──
        # RIDEV_INPUTSINK 让这个**隐藏、无焦点**的窗口也能收到 WM_INPUT
        # （2026-10-13 实测：15s 收到 322 条，见 probes/probe_rawinput.py）。
        # 失败不致命：退化成纯轮询（只是复活慢几秒），不影响任何既有功能。
        try:
            _register_raw_mouse(user32, hwnd)
        except Exception as exc:  # noqa: BLE001
            log.debug("theme watch: RegisterRawInputDevices 失败: %s", exc)
        log.debug("theme watch window up (current dark=%s)", state["dark"])
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=_loop, name="theme-watch", daemon=True).start()


def _register_usb_notify(user32, hwnd) -> None:
    """给窗口注册 USB 设备接口通知（DEVICE_NOTIFY_WINDOW_HANDLE）。

    实测（`probes/probe_device_events2.py`）：注册后能精确收到
    `DBT_DEVICEARRIVAL` / `DBT_DEVICEREMOVECOMPLETE`，与设备树变化分开。
    这里只注册 USB 接口这一个类别 —— 本项目的目标设备（接收器/键盘/鼠标）
    都是 USB 形态，不需要监听卷/端口等其它类别。
    """
    import ctypes
    from ctypes import wintypes

    class DEV_BROADCAST_DEVICEINTERFACE(ctypes.Structure):
        _fields_ = [("dbcc_size", wintypes.DWORD),
                    ("dbcc_devicetype", wintypes.DWORD),
                    ("dbcc_reserved", wintypes.DWORD),
                    ("dbcc_classguid", ctypes.c_ubyte * 16),
                    ("dbcc_name", ctypes.c_wchar * 1)]

    # {A5DCBF10-6530-11D2-901F-00C04FB951ED} = GUID_DEVINTERFACE_USB_DEVICE
    GUID_USB = (ctypes.c_ubyte * 16)(*[
        0x10, 0xBF, 0xDC, 0xA5, 0x30, 0x65, 0xD2, 0x11,
        0x90, 0x1F, 0x00, 0xC0, 0x4F, 0xB9, 0x51, 0xED])
    DBT_DEVTYP_DEVICEINTERFACE = 5
    DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000

    dbi = DEV_BROADCAST_DEVICEINTERFACE()
    dbi.dbcc_size = ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE)
    dbi.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE
    ctypes.memmove(ctypes.byref(dbi.dbcc_classguid), GUID_USB, 16)
    user32.RegisterDeviceNotificationW.restype = wintypes.HANDLE
    user32.RegisterDeviceNotificationW.argtypes = (
        wintypes.HWND, wintypes.LPVOID, wintypes.DWORD)
    hn = user32.RegisterDeviceNotificationW(
        hwnd, ctypes.byref(dbi), DEVICE_NOTIFY_WINDOW_HANDLE)
    log.debug("USB 设备通知已注册 handle=%s", hn)
    # 持强引用：结构体被 GC 后内核回调可能读到悬垂内存
    _usb_notify_keep.append(dbi)


_usb_notify_keep: list = []   # DEV_BROADCAST_DEVICEINTERFACE 强引用防 GC


def _register_raw_mouse(user32, hwnd) -> None:
    """给窗口注册 RAWINPUT 鼠标监听（RIDEV_INPUTSINK，2026-10-13）。

    ── 为什么这样注册 ──
    * `usUsagePage=0x01`（GENERIC_DESKTOP）/ `usUsage=0x02`（MOUSE）—— 只要鼠标，
      不要键盘（键盘原始输入会混进大量按键噪音，本项目不需要）；
    * `RIDEV_INPUTSINK`（0x100）—— **关键**：让窗口在**无焦点/后台**时也收输入。
      agent 常驻托盘、永远没有焦点，不加这个标志就一条 WM_INPUT 都收不到；
    * `hwndTarget=hwnd` —— 输入投递到这个隐藏消息窗口。

    实测（`probes/probe_rawinput.py`）：隐藏窗口 + INPUTSINK 15s 收到 322 条
    WM_INPUT，全部 type=mouse ⇒ 方案在本机成立。

    只注册鼠标注册表（不碰键盘/HID）：少一份噪音，也就少一份解析开销。
    """
    import ctypes
    from ctypes import wintypes

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", ctypes.c_ushort),
                    ("usUsage", ctypes.c_ushort),
                    ("dwFlags", wintypes.DWORD),
                    ("hwndTarget", wintypes.HWND)]

    HID_USAGE_PAGE_GENERIC = 0x01
    HID_USAGE_GENERIC_MOUSE = 0x02
    RIDEV_INPUTSINK = 0x00000100

    rr = user32.RegisterRawInputDevices
    rr.restype = wintypes.BOOL
    rr.argtypes = (ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT)

    rid = RAWINPUTDEVICE()
    rid.usUsagePage = HID_USAGE_PAGE_GENERIC
    rid.usUsage = HID_USAGE_GENERIC_MOUSE
    rid.dwFlags = RIDEV_INPUTSINK
    rid.hwndTarget = hwnd
    ok = rr(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE))
    if ok:
        log.info("RAWINPUT 鼠标监听已注册（后台无焦点生效，唤醒即刷新）")
    else:
        log.debug("RegisterRawInputDevices 返回 0, err=%s", ctypes.GetLastError())
    # 持强引用：结构体被 GC 后内核读悬垂内存（与 USB notify 同款纪律）
    _raw_input_keep.append(rid)


_raw_input_keep: list = []    # RAWINPUTDEVICE 强引用防 GC


def _query_quns() -> int | None:
    """查一次 `SHQueryUserNotificationState`（全屏/游戏感知的信号源）。

    返回 `QUNS_*` 整数（见 `fullscreen.py`），查不到返回 **None**。
    纯只读、极廉价（实测单次 <0.1ms），可以在 2s 节拍上放心调。

    ── 为什么要它 ──
    这是 Windows 自己"要不要弹通知"时用的**权威判据**，比"看前台窗口是否
    占满屏幕"准得多（后者要处理多显示器、DPI、置顶/工具窗口一堆边界）。本机
    实测（`probes/probe_quns_fullscreen.py`）：无边框全屏窗口获得焦点 → 返回
    `QUNS_BUSY`(2)；关掉 → 立刻回 `QUNS_ACCEPTS_NOTIFICATIONS`(5)。

    ⚠️ **绝不能只认 `QUNS_RUNNING_D3D_FULL_SCREEN`**：实测无边框全屏（游戏最
    常见形态）返回的是 `BUSY`。判定统一走 `fullscreen.is_dont_disturb()`。
    """
    import ctypes
    try:
        f = ctypes.windll.shell32.SHQueryUserNotificationState
        f.restype = ctypes.c_long
        f.argtypes = (ctypes.POINTER(ctypes.c_int),)
        state = ctypes.c_int(-1)
        hr = f(ctypes.byref(state))
        if hr != 0:                      # S_OK == 0
            log.debug("SHQueryUserNotificationState hr=%s", hr)
            return None
        return int(state.value)
    except Exception as exc:  # noqa: BLE001 - 感知失败不该影响任何功能
        log.debug("SHQueryUserNotificationState unavailable: %s", exc)
        return None


def _enable_dark_menus() -> None:
    """让原生 TrackPopupMenu 菜单跟随 Win11 系统配色：深色模式黑底白字、
    浅色模式白底黑字（经典 light 菜单与系统风格脱节）。

    Win11 判定「该进程的菜单是否走深色主题引擎」的条件有两个，python.exe
    都不满足，所以默认拿到经典浅色菜单：
      1) 进程启用 Visual Styles（comctl32 v6 manifest）——运行时用
         CreateActCtx 激活一个内存 manifest 补上；
      2) 公共控件深色模式开关 —— uxtheme.dll 未导出序号 135
         SetPreferredAppMode + 136 FlushMenuThemes（社区方案 darkmenu.c
         同款，Win11 21H2+ 有效）。
    模式选择：AllowDark(1) 只跟随系统「应用模式」，与用户要的「跟随
    Windows 模式」不是一回事——因此默认「auto」：读注册表
    SystemUsesLightTheme 判 Windows 模式深浅后 ForceDark(2)/ForceLight(3)，
    与任务栏/开始菜单/音量面板观感一致。
    BATBAR_MENU_THEME 可覆盖：auto(默认) / dark(无条件深) / light(无条件浅)。
    成功后起监听线程，运行中切换系统配色时菜单实时跟随。
    任何一步失败都静默跳过（菜单保持经典浅色，不影响功能）。"""
    global _set_preferred_app_mode, _flush_menu_themes
    import ctypes
    import os
    from ctypes import wintypes
    try:
        kernel32 = ctypes.windll.kernel32
    except AttributeError:      # 非 Windows（单测环境）
        return

    # --- 1) activation context：临时 manifest 声明依赖 Common-Controls v6 ---
    _MANIFEST = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">\n'
        ' <dependency>\n'
        '  <dependentAssembly>\n'
        '   <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls"'
        ' version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df"'
        ' language="*"/>\n'
        '  </dependentAssembly>\n'
        ' </dependency>\n'
        '</assembly>\n'
    )

    class ACTCTX(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD),
                    ("lpSource", wintypes.LPCWSTR), ("lpProcedureName", wintypes.LPCWSTR),
                    ("lpAssemblyDirectory", wintypes.LPCWSTR), ("lpResourceName", wintypes.LPCWSTR),
                    ("wLangId", wintypes.WORD), ("hModule", wintypes.HMODULE),
                    ("lpAssemblySecurityAttributes", wintypes.LPVOID)]

    try:
        import tempfile
        mdir = tempfile.mkdtemp(prefix="batbar-manifest-")
        mpath = os.path.join(mdir, "batbar.manifest")
        with open(mpath, "w", encoding="utf-8") as f:
            f.write(_MANIFEST)
        ctx = ACTCTX(cbSize=ctypes.sizeof(ACTCTX), dwFlags=0,
                     lpSource=mpath)
        kernel32.CreateActCtxW.restype = wintypes.HANDLE
        kernel32.CreateActCtxW.argtypes = (ctypes.POINTER(ACTCTX),)
        handle = kernel32.CreateActCtxW(ctypes.byref(ctx))
        INVALID = ctypes.cast(ctypes.c_void_p(-1), ctypes.c_void_p).value
        if handle not in (None, 0, INVALID):
            cookie = ctypes.c_size_t(0)
            if kernel32.ActivateActCtx(handle, ctypes.byref(cookie)):
                log.debug("visual-styles activation context ok")
            else:
                log.debug("ActivateActCtx failed (rc=%s)", ctypes.GetLastError())
    except Exception as exc:  # noqa: BLE001
        log.debug("manifest actctx: %s", exc)

    # --- 2) uxtheme 序号 API：允许深色 + 刷新菜单主题缓存 ---
    # 这两个导出没有名字，只能按序号取（darkmenu.c 同款）：
    #   135 = SetPreferredAppMode(int) -> int   136 = FlushMenuThemes(void)
    # MAKEINTRESOURCE 语义 = 把序号本身当 LPCSTR 传给 GetProcAddress。
    try:
        ux = ctypes.windll.uxtheme
        getproc = kernel32.GetProcAddress
        getproc.restype = ctypes.c_void_p
        getproc.argtypes = (wintypes.HMODULE, ctypes.c_void_p)
        p135 = getproc(ux._handle, 135)
        p136 = getproc(ux._handle, 136)
        if p135 and p136:
            _set_preferred_app_mode = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int)(p135)
            _flush_menu_themes = ctypes.WINFUNCTYPE(None)(p136)
            # 0=Default 1=AllowDark(只跟系统"应用模式") 2=ForceDark 3=ForceLight
            # auto（默认）= 自读注册表判系统深浅再 Force，混搭主题下与
            # shell 观感一致；dark/light 强制单向。
            mode = (os.environ.get("BATBAR_MENU_THEME") or "auto").strip().lower()
            if mode == "dark":
                _apply_menu_dark(True)
            elif mode == "light":
                _apply_menu_dark(False)
            else:
                dark = _detect_sys_dark()
                _apply_menu_dark(dark)
                log.info("menu theme auto: sys dark=%s", dark)
                try:
                    _start_theme_watch()
                except Exception as exc:  # noqa: BLE001
                    log.debug("theme watch unavailable: %s", exc)
        else:
            log.debug("dark menus: ordinal exports not found (Win < 11?)")
    except Exception as exc:  # noqa: BLE001
        log.debug("dark menus unavailable: %s", exc)


def main():
    _enable_dpi_awareness()   # 必须最先：建托盘窗口/菜单之前
    # ★ 打包版（windowed exe）没有控制台：`sys.stdout` 是 None，而 CPython 的
    #   print 对 None 是**静默 no-op** ⇒ `batbar.exe --scan` 会一个字都不打印。
    #   这里把调用方的控制台接回来。必须早于 applog.setup() —— 后者靠「stdout
    #   是不是 None」来决定装不装控制台 handler，晚了就永远只剩文件日志。
    attach_parent_console()
    # ★ 控制台编码兜底：中文 cmd 的 stdout 是 cp936，写 emoji 会抛
    #   UnicodeEncodeError 并**直接终止进程**（--scan 实测栽在「✅ 可用写长度」
    #   那一行）。放在 attach 之后 —— 它可能刚把 stdout 换成新的对象。
    make_console_safe()
    # ★ cwd 锚定（2026-10-13）：开机自启/计划任务的 cwd 可能是 System32。
    #   放在 argparse 之前 —— 任何文件访问都可能受 cwd 影响，越早锚定越好。
    #   日志此刻还没装好，所以这里**不能**打日志，靠返回值和 _boot 打点体现。
    _cwd = anchor_cwd()
    ap = argparse.ArgumentParser(prog="batbar-agent")
    ap.add_argument("--device", default=None, help="蓝牙直连设备名关键字（USB 接收器用户不需要）")
    ap.add_argument("--standalone", action="store_true",
                    help="Windows 侧自己轮询+渲染+托盘（验证/轻量部署）")
    ap.add_argument("--probe", action="store_true",
                    help="只读一次电量打印结果就退出，验证 HID++ 协议对你的设备是否生效")
    ap.add_argument("--scan", action="store_true",
                    help="诊断：枚举所有罗技 collection 并逐槽位探测，定位正确的 usage_page+device_index")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--allow-multiple", action="store_true",
                    help="调试用：跳过单实例检查（两个实例会抢同一条 HID 输入队列）")
    args = ap.parse_args()
    # ★ 历史数据搬迁（2026-10-13）：落盘位置走过两代（logi-tray → batbar →
    #   现在的绿色模式「跟 exe 同目录」）。把老位置的文件**复制**进当前数据目录
    #   （只复制不移动、目标已有就不覆盖、源目录保留）。必须在 applog.setup()
    #   **之前** —— 要搬的东西之一就是日志文件本身；失败只静默降级（等价于没搬），
    #   结果先存着，等 setup 后补打一行。
    #   ★ 它又必须晚于 parse_args()：`--help` / 参数敲错时 argparse 直接退出，
    #   这时**不该有任何落盘副作用**。这不是洁癖 —— CI 的冒烟步骤正是拿
    #   `--help` 真跑一次 exe，而绿色模式下数据目录 == exe 所在目录（= 发布
    #   目录），迁移一跑就把 %LOCALAPPDATA%\batbar 的历史电量表复制进了发布
    #   目录，随后被打进 zip 发出去（2026-10-13 首次构建 v0.1.2 真踩到）。
    from . import migrate
    _migrated, _migrate_why = migrate.migrate()
    from . import applog, singleinstance, paths
    path = applog.setup("agent", verbose=args.verbose)
    if path is not None:
        log.info("日志文件：%s", path)
    # ★ 绿色模式：数据（logs / last-known.json / agent.pid）默认落在 exe 同目录。
    #   这一行是排障的第一入口 —— 用户问"数据在哪"时，日志里直接有答案。
    log.info("%s", paths.note())
    _boot("日志就绪")
    # ★ 未捕获异常落盘：windowed exe 里 traceback 无处可去（用户只看到"跑一半
    #   没了"）。必须晚于 applog.setup() —— 早装的话日志还没有文件 handler。
    install_crash_log()
    log.debug("cwd 已锚定：%s", _cwd)
    if _migrated:
        log.info("数据目录迁移：%s", _migrate_why)
    else:
        log.debug("数据目录迁移：%s", _migrate_why)
    # ★ dark menus / theme watch 必须在**日志装好之后**调：否则它的日志
    #   （含「RAWINPUT 鼠标监听已注册」）会落在未配置的 logger 上全部丢失，
    #   出问题只能盲猜。仍在建托盘菜单之前（本句先于 Agent 构造 / run）。
    _enable_dark_menus()
    _boot("深色菜单+事件监听已就绪")
    # ★ 单实例：两个 agent 会各开一个句柄抢同一条 HID 输入队列，谁也读不全
    if not args.allow_multiple and not singleinstance.acquire():
        raise SystemExit(1)
    _boot("单实例锁已占")
    # PID 文件：给 restart-diag.bat 用 —— 旧脚本靠 `Get-CimInstance
    # Win32_Process` 全进程枚举来杀旧实例，那一步实测 3~8 秒，正是「双击后
    # 五六秒才看到托盘」的真凶。写了 PID 就能 `taskkill /PID` 秒杀。
    pid_file = singleinstance.write_pid()
    if pid_file is not None:
        log.debug("pid file: %s", pid_file)
    a = Agent(args.device)
    _boot("Agent 构造完成")
    if args.scan:
        raise SystemExit(_scan(a))
    if args.probe:
        raise SystemExit(_probe(a))
    a.run(standalone=args.standalone)


if __name__ == "__main__":
    main()
