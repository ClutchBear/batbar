"""落盘位置的**唯一真相源**（2026-10-13：改成绿色模式）。

为什么要有这个模块
------------------
之前 `applog` / `lastknown` / `singleinstance` / `migrate` **各自拼一遍**
`%LOCALAPPDATA%\\batbar`，四处独立实现同一个概念 —— 改一处忘一处的概率不低
（"数据到底写哪" 这种问题最怕不一致）。现在统一从这里取。

落盘策略（绿色 / 便携优先）
---------------------------
1. `BATBAR_DATA_DIR` 环境变量 —— 最高优先级，测试与特殊部署用。
2. **打包后（frozen）**：`batbar.exe` **所在目录**，也就是解压出来那个文件夹。
   数据不再写 `%LOCALAPPDATA%`，整个文件夹拷走就能带走全部数据（绿色软件）。
   实测落点（2026-10-13）：
       D:\\batbar\\batbar.exe
       D:\\batbar\\logs\\agent.log
       D:\\batbar\\last-known.json
       D:\\batbar\\agent.pid
3. **源码运行（开发/测试）**：保持 `%LOCALAPPDATA%\\batbar` —— 否则会在仓库根
   目录里拉屎（日志、缓存混进 `git status`）。
4. 兜底：`%LOCALAPPDATA%` 也拿不到 → 用户目录下的 `.batbar`。

★ 不可写就回退
--------------
绿色模式有个硬约束：**exe 所在目录不一定可写**（解压到 `C:\\Program Files\\`、
只读盘、只读网络共享、被安全软件锁住……）。所以 frozen 分支会先做一次
**可写性探测**（真的建目录 + 写一个探针文件再删掉），不可写就回退到
`%LOCALAPPDATA%\\batbar`，并把原因记进 `note()`，由 `agent.main()` 在日志装好
之后打出来。**绝不因为不可写就不落盘** —— 没日志的托盘程序等于瞎跑。

放置约定：`logs` / `last-known.json` / `agent.pid` **同级平铺**在数据目录里。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("batbar.paths")

#: 数据目录环境变量（测试 / 特殊部署）
ENV_DIR = "BATBAR_DATA_DIR"
#: 日志目录单独重定向（兼容既有约定，优先级高于本模块的推导）
ENV_LOG_DIR = "BATBAR_LOG_DIR"

APP_DIR = "batbar"  # 回退目录名（%LOCALAPPDATA%\batbar）
PROBE_NAME = ".batbar-writable-probe"

_cache: dict[tuple, Path] = {}  # 按「输入签名」缓存，见 _signature()
_notes: dict[tuple, str] = {}
_note: str | None = None  # 最近一次落点决策的人类可读说明


def _signature() -> tuple:
    """本次推导的全部输入。

    ★ 缓存**必须按输入签名**做，不能只存一个值：环境变量变了（测试里 patch、
    便携部署改配置）而返回旧路径，是最阴的一类 bug —— 本仓库单测里实际踩过
    （一个用例把 `LOCALAPPDATA` 清空调用一次，缓存住"用户目录"落点，污染了后面
    另一个用例的断言）。签名里带上 frozen 与 exe 路径，顺带让
    `sys.executable` 被 mock 的测试也自动失效。
    """
    frozen = is_frozen()
    return (
        os.environ.get(ENV_DIR),
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
        frozen,
        str(sys.executable) if frozen else "",
    )


def _home_dir() -> Path:
    """最后的兜底：`~/batbar`。

    名字沿用 `APP_DIR`（不叫 `.batbar`）—— 全项目只留一个目录名，省得将来有人
    照着文档找 `batbar` 却看见 `.batbar`。这条路径在 Windows 上基本走不到
    （`LOCALAPPDATA` 总是存在），它只是"总得落个盘"的保险。
    """
    return Path(os.path.expanduser("~")) / APP_DIR


def legacy_dir() -> Path:
    """`%LOCALAPPDATA%\\batbar`（无环境变量时的回退点，也是旧数据的来源）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR
    return _home_dir()


def is_frozen() -> bool:
    """是否跑在 PyInstaller 打出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> Path | None:
    """exe 所在目录（绿色模式的落点）；不是 frozen 则 None。

    注意**不是** `sys.base_prefix` / `_MEIPASS`：onefile 的解包目录是临时目录，
    锚在那儿数据会随进程退出被清掉（打包时踩过，见 ARCHITECTURE 铁律）。

    这里 `resolve()` 是**故意**的，别顺手删：Windows 上 `sys.executable` 可能是
    8.3 **短名**（`C:\\Users\\RUNNER~1\\...`），resolve 一次就成规范长名 ——
    日志里好读，跨模块比较也稳定。代价是"经符号链接启动"时会跟到真实目录，
    对绿色程序可以接受（用户拿到的是同一个文件夹）。相关单测：
    `tests/test_paths.py::test_frozen_exe_dir_survives_83_short_names`。
    """
    if not is_frozen():
        return None
    try:
        return Path(sys.executable).resolve().parent
    except OSError:
        return Path(sys.executable).parent


def _writable(d: Path) -> bool:
    """能不能往 d 里写东西 —— 真建一次目录 + 写个探针文件再删。

    别用 `os.access(W_OK)`：在 Windows 上它对 ACL 与 UAC 虚拟化的判断
    并不可靠（Program Files 下常常报"可写"）。真写一次最实在。
    """
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / PROBE_NAME
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001 - 探测失败就是不可写
        return False


def data_dir() -> Path:
    """当前生效的数据目录（按输入签名缓存；`reset_cache()` 可强制重算）。"""
    global _note
    sig = _signature()
    if sig in _cache:
        _note = _notes.get(sig)
        return _cache[sig]

    env = os.environ.get(ENV_DIR)
    if env:
        p = Path(env)
        _note = f"数据目录 = {p}（来自环境变量 {ENV_DIR}）"
    else:
        d = exe_dir()
        if d is not None:
            if _writable(d):
                p = d
                _note = f"数据目录 = {d}（绿色模式：跟着 exe 走）"
            else:
                p = legacy_dir()
                _note = f"数据目录 = {p}（**exe 所在目录不可写**：{d} ⇒ 回退到用户目录）"
                log.warning("exe 目录不可写（%s），数据回退到 %s", d, p)
        else:
            p = legacy_dir()
            _note = f"数据目录 = {p}（源码运行，走用户目录）"

    _cache[sig] = p
    _notes[sig] = _note
    return p


def logs_dir() -> Path:
    """日志目录。`BATBAR_LOG_DIR` 仍可单独覆盖（既有约定）。"""
    env = os.environ.get(ENV_LOG_DIR)
    if env:
        return Path(env)
    return data_dir() / "logs"


def lastknown_path() -> Path:
    """最后已知电量表的落盘文件。"""
    return data_dir() / "last-known.json"


def pid_path() -> Path:
    """单实例 PID 文件。"""
    return data_dir() / "agent.pid"


def note() -> str:
    """最近一次落点决策的说明（`applog.setup()` 之后打日志用）。"""
    if _note is not None:
        return _note
    return f"数据目录 = {data_dir()}"


def reset_cache() -> None:
    """清掉缓存 —— 强制重算（可写性探测也要重来一次时用）。"""
    global _note
    _cache.clear()
    _notes.clear()
    _note = None
