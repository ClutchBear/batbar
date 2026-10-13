"""把历史位置的落盘数据搬进当前数据目录。

背景
----
落盘位置经历了两代变化：

  ① 最初叫 **logi-tray** → 数据在 `%LOCALAPPDATA%\\logi-tray\\`
  ② 2026-10-13 改名 **batbar** → 数据在 `%LOCALAPPDATA%\\batbar\\`
  ③ 2026-10-13 晚改成**绿色模式** → 打包后数据落在 **`batbar.exe` 同目录**

改名的后果是「电量表清空 + 日志断档」：菜单上设备会闪一下，出问题也看不到
上一代留下什么。所以每一代都要能把上一代的数据接过来。

要搬的东西（三样，都是跨重启有价值的）
--------------------------------------
  * `logs\\agent.log*`  —— 历史排障日志（滚动 1 MB × 2）
  * `last-known.json`   —— 最后已知电量表（蓝牙回落 / 2.4G 场景的兜底读数）
  * ~~`agent.pid`~~     —— **故意不搬**：那是瞬时状态，进程启动时本来就会重写；
    而把上一代的旧 PID 抄过来，万一那个 PID 已被别的进程复用，重启脚本
    `taskkill /F /PID <n>` 就会误杀无辜进程。

设计取舍
--------
1. **只复制、不移动、不删源**（2026-10-13 用户确认）—— 绿色模式是「换个位置
   再跑一份」，旧目录留着可随时回退。搬完旧目录原样在那儿，代价几十 KB。
2. **逐文件搬，目标已有同名文件就跳过** —— 绝不覆盖。目标里已有的数据必然比
   旧位置的更新（否则它就是刚被搬进来的），覆盖只会把新日志冲成旧日志。
3. **越新的来源越先处理** —— `legacy_dirs()` 是「最老 → 上一代」的顺序，这里
   反过来遍历：`batbar`（上一代）先搬，`logi-tray`（最老）后搬。因为目标是
   「先占先得、已存在就跳过」，所以新一代的数据不会被老一代顶掉。
   ⚠️ 反了就是 bug（顺序写错过一次）：老的先搬会占住目标，把新的挤掉。
4. **不做重定向嗅探** —— 旧版用 `LOGI_*` 环境变量、新版用 `BATBAR_*`，互不相干；
   用户若当年重定向过，这些历史目录本就不存在，自然跳过。
5. **失败一律静默** —— 迁移失败（权限、文件被占用、跨卷）只写进返回值，
   **绝不抛异常**拖垮启动。最坏情况等价于「没搬」。

调用时机：`agent.main()` 里 `anchor_cwd()` 之后、`applog.setup()` **之前**
（要搬的东西之一就是日志文件本身，得先搬完再装 handler）。此刻日志还没装好，
所以本模块不自己打日志，而是把结果**返回**给调用方，由调用方在 `applog.setup()`
之后补打一行。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

#: 历史目录名（越靠后越新）
LEGACY_DIRNAMES = ("logi-tray", "batbar")
#: 要搬的散装文件
COPY_FILES = ("last-known.json",)
#: 要搬的目录（里面的文件逐个搬，目标同名则跳过）
COPY_DIRS = ("logs",)
#: 日志文件的通配（含轮转的 .1 / .2）
LOG_GLOBS = ("agent.log*",)


def _localappdata() -> Path | None:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return Path(base) if base else None


def legacy_dirs() -> list[Path]:
    """历史数据目录（**越新越靠后**）；拿不到 `%LOCALAPPDATA%` 返回空表。"""
    base = _localappdata()
    if base is None:
        return []
    return [base / name for name in LEGACY_DIRNAMES]


def _copy_file(src: Path, dst: Path) -> bool:
    """把 src 复制成 dst（**dst 已存在就不动**）。返回是否真的复制了。"""
    try:
        if dst.exists() or not src.is_file():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)          # copy2：连 mtime 一起带过去
        return True
    except Exception:                   # noqa: BLE001 - 单个文件失败不影响其它
        return False


def _copy_from(src: Path, dst: Path) -> list[str]:
    """把 src 目录里该搬的东西搬进 dst，返回搬成功的文件名表。"""
    done: list[str] = []
    for name in COPY_FILES:
        if _copy_file(src / name, dst / name):
            done.append(name)
    for sub in COPY_DIRS:
        s = src / sub
        if not s.is_dir():
            continue
        for pat in LOG_GLOBS if sub == "logs" else ("*",):
            for f in sorted(s.glob(pat)):
                if _copy_file(f, dst / sub / f.name):
                    done.append(f"{sub}/{f.name}")
    return done


def migrate(old: str | os.PathLike[str] | None = None,
            new: str | os.PathLike[str] | None = None) -> tuple[bool, str]:
    """把历史数据目录里的文件搬进当前数据目录。

    Args:
        old: 显式指定**单个**来源目录（测试用）。不传则遍历 `legacy_dirs()`。
        new: 显式指定目标目录（测试用）；默认 `paths.data_dir()`
             （绿色模式下就是 exe 所在目录）。

    Returns:
        `(是否真的搬了东西, 人类可读的结果描述)` —— 供调用方打日志 / 单测断言。
        任何异常都被吞掉并返回 `(False, 原因)`，**绝不抛**。
    """
    try:
        if new is None:
            from . import paths

            dst = Path(paths.data_dir())
        else:
            dst = Path(new)

        if old is not None:
            srcs = [Path(old)]
        else:
            srcs = legacy_dirs()
            if not srcs:
                return False, "拿不到 %LOCALAPPDATA%/%APPDATA%，跳过迁移"
            # ★ 最新的来源先搬（目标先占先得，见模块 docstring 第 3 条）
            srcs = list(reversed(srcs))

        # 只搬比目标"更老位置"的数据；目标自己就是某个历史目录时（源码运行
        # 的默认情形）自然落进这条分支 → 等价于 no-op，保持既有行为不变。
        try:
            dst_r = dst.resolve()
        except OSError:
            dst_r = dst

        done: list[str] = []
        skipped_same: list[str] = []
        for s in srcs:
            if not s.is_dir():
                continue
            try:
                if s.resolve() == dst_r:
                    skipped_same.append(str(s))
                    continue
            except OSError:
                pass
            done.extend(_copy_from(s, dst))

        if done:
            return True, (f"已从历史目录复制 {len(done)} 个文件到 {dst}："
                          f"{', '.join(done[:6])}{' …' if len(done) > 6 else ''}"
                          f"（源目录保留未删）")
        if skipped_same:
            return False, f"数据目录即原始位置（{skipped_same[0]}），无需迁移"
        return False, "没有可迁移的历史数据（全新用户，或早已搬过）"
    except Exception as exc:                       # noqa: BLE001 - 迁移失败不致命
        return False, f"迁移失败（已忽略）：{exc}"
