"""日志落盘：常驻托盘程序没有控制台窗口，出问题只能靠猜，所以必须写文件。

默认落点：**打包后 = `batbar.exe` 同目录下的 `logs\\<name>.log`**（绿色模式），
源码运行 = `%LOCALAPPDATA%\\batbar\\logs\\<name>.log`；滚动 1 MB × 2 份，UTF-8。
（落点推导全在 `paths.py`，本模块不自己拼路径。）

级别：
  默认 INFO；`--verbose` 或环境变量 `BATBAR_DEBUG=1` 开 DEBUG。

⚠️ 为什么不用 basicConfig：托盘程序通常从 `wscript agent-hidden.vbs` 静默启动，
stdout 根本不存在。而且我们既要保留控制台（前台调试要看），又要在静默时
有文件可查 —— 所以这里显式装两个 handler，并对「重复 setup」做幂等处理
（换掉旧 handler，而不是叠加，否则每调用一次日志就重复一行）。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

log = logging.getLogger("batbar.applog")

APP_DIR = "batbar"               # 回退目录名（源码运行时的 %LOCALAPPDATA%\batbar）
ENV_DEBUG = "BATBAR_DEBUG"
#: 单独重定向日志目录（测试 / 便携部署）。完整落盘策略见 `paths.py`。
ENV_DIR = "BATBAR_LOG_DIR"

DEFAULT_MAX_BYTES = 1 << 20    # 1 MB
DEFAULT_BACKUPS = 2
_FMT = "%(asctime)s %(name)s %(levelname)s %(message)s"

# 第三方库的 DEBUG 是纯噪声：Pillow 每次渲染图标会打几十行 "STREAM b'IHDR'"，
# 开 -v 时几秒就能把 1 MB 日志滚没，真正有用的 batbar.* 记录反而被冲掉。
NOISY_LOGGERS = ("PIL", "PIL.Image", "PIL.PngImagePlugin", "urllib3", "asyncio")

_MARK = "_batbar_app_handler"     # 给本模块装的 handler 打标记，便于幂等替换


def default_dir() -> Path:
    """日志目录 —— 委托给 `paths.logs_dir()`（落盘位置的唯一真相源）。

    优先级：`BATBAR_LOG_DIR` > `BATBAR_DATA_DIR`/绿色模式 > `%LOCALAPPDATA%`。
    打包后是 **exe 同目录下的 `logs\\`**（绿色软件形态，2026-10-13 改）。
    """
    from . import paths

    return paths.logs_dir()


def debug_enabled(verbose: bool = False) -> bool:
    """是否开 DEBUG 级：--verbose 或 BATBAR_DEBUG=1。"""
    if verbose:
        return True
    return os.environ.get(ENV_DEBUG, "").strip() not in ("", "0", "false", "no")


def setup(name: str = "agent", verbose: bool = False, *,
          log_dir: str | os.PathLike[str] | None = None,
          max_bytes: int = DEFAULT_MAX_BYTES,
          backups: int = DEFAULT_BACKUPS,
          to_console: bool = True) -> Path | None:
    """装好 root logger 的文件 + 控制台输出。

    Returns:
        日志文件路径；落盘失败（权限/路径不可写）返回 None —— **不抛异常**，
        日志写不成不能拖垮托盘程序。
    """
    level = logging.DEBUG if debug_enabled(verbose) else logging.INFO
    fmt = logging.Formatter(_FMT)
    root = logging.getLogger()
    root.setLevel(level)
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 幂等：先摘掉上一次 setup 装的 handler（文件 + 控制台都带 _MARK）
    for h in list(root.handlers):
        if getattr(h, _MARK, False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:              # noqa: BLE001
                pass

    path: Path | None = None
    if log_dir is not None:
        d = Path(log_dir)
    else:
        d = default_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name}.log"
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        setattr(fh, _MARK, True)
        root.addHandler(fh)
    except Exception as exc:               # noqa: BLE001 - 日志失败不该致命
        log.warning("日志文件不可用（%s）：%s —— 仅输出到控制台", d, exc)
        path = None

    # 静默启动（pythonw / agent-hidden.vbs）时 stdout 与 stderr 都是 None，
    # 此时装 StreamHandler 会在 emit 时炸 —— 直接不装。
    stream = sys.stdout if sys.stdout is not None else sys.stderr
    if to_console and stream is not None and not any(
            getattr(h, _MARK, False) and isinstance(h, logging.StreamHandler)
            for h in root.handlers):
        sh = logging.StreamHandler(stream)
        sh.setFormatter(fmt)
        sh.setLevel(level)
        setattr(sh, _MARK, True)
        root.addHandler(sh)

    return path


def current_path() -> Path | None:
    """当前 root logger 上本模块装的日志文件路径；没落盘则返回 None。"""
    for h in logging.getLogger().handlers:
        if getattr(h, _MARK, False) and hasattr(h, "baseFilename"):
            return Path(h.baseFilename)
    return None
