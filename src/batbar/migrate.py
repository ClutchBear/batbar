"""旧版本数据目录自动迁移（logi-tray → batbar）。

背景
----
本项目 2026-10-13 从 **logi-tray** 改名为 **batbar**。落盘数据目录也跟着
从 `%LOCALAPPDATA%\\logi-tray\\` 换到 `%LOCALAPPDATA%\\batbar\\`，里面有三样
**跨重启有价值**的东西：

  * `logs\\agent.log*`  —— 历史排障日志（滚动 1 MB × 2）
  * `last-known.json`   —— 最后已知电量表（蓝牙回落 / 2.4G 场景的兜底读数）
  * `agent.pid`         —— 上次运行的 PID（重启脚本秒杀用；顺手一起搬）

不迁移的后果：改名后第一次启动，电量表清空 → 菜单短暂闪烁；日志断档 →
出问题看不到「改名那一刻发生过什么」。所以这里做一次性搬迁。

设计取舍
--------
1. **只搬不合并** —— 用 `shutil.move` 把旧目录**整棵**移到新目录，而不是逐文件
   复制。搬完旧目录消失，天然幂等：第二次跑发现旧目录不在，直接 no-op。
2. **新目录已存在就不动** —— 若 `%LOCALAPPDATA%\\batbar\\` 已存在（说明用户
   已跑过新版、产生了新数据），**绝不合并覆盖**。理由：合并两代
   `last-known.json` 需要按时间戳仲裁，收益极低而写坏风险不低；日志目录合并
   更易撞名。此时只在日志里留一行提示，人肉处理。
3. **不做重定向判断** —— 旧版本用的是 `LOGI_LOG_DIR` / `LOGI_LASTKNOWN`，与新版
   `BATBAR_*` 无关；若用户当年重定向过，`%LOCALAPPDATA%\\logi-tray` 本就不存在，
   会被第 ① 条自然跳过。若用户**现在**设了 `BATBAR_LOG_DIR`，那新版日志写别处、
   搬走旧目录同样无害。所以无需、也不该在这里嗅探环境变量。
4. **失败一律静默** —— 搬迁失败（权限、文件被占用、跨卷异常）只把原因写进
   返回值，**绝不抛异常**拖垮启动。最坏情况等价于「没搬」，退化成原状。

调用时机：`agent.main()` 里 `anchor_cwd()` 之后、`applog.setup()` **之前**
（要搬的东西之一就是日志目录本身，得先搬完再装 handler）。因为此刻日志还没
装好，本模块不自己打日志，而是把结果**返回**给调用方，由调用方在
`applog.setup()` 之后补打一行。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

OLD_DIRNAME = "logi-tray"
NEW_DIRNAME = "batbar"


def _default_dirs() -> tuple[Path, Path] | None:
    """推导默认的新旧目录；拿不到 `%LOCALAPPDATA%` 返回 None。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        return None
    b = Path(base)
    return b / OLD_DIRNAME, b / NEW_DIRNAME


def migrate(old: str | os.PathLike[str] | None = None,
            new: str | os.PathLike[str] | None = None) -> tuple[bool, str]:
    """把旧数据目录整棵搬到新数据目录。

    Args:
        old: 显式指定旧目录（测试用）；默认 `%LOCALAPPDATA%\\logi-tray`。
        new: 显式指定新目录（测试用）；默认 `%LOCALAPPDATA%\\batbar`。

    Returns:
        `(是否真的搬了, 人类可读的结果描述)` —— 供调用方打日志 / 单测断言。
        任何异常都被吞掉并返回 `(False, 原因)`，**绝不抛**。
    """
    try:
        if old is None or new is None:
            dirs = _default_dirs()
            if dirs is None:
                return False, "拿不到 %LOCALAPPDATA%/%APPDATA%，跳过迁移"
            d_old, d_new = dirs
            old = old if old is not None else d_old
            new = new if new is not None else d_new

        src, dst = Path(old), Path(new)

        # ① 旧目录不存在 → 全新用户 / 已经搬过。最正常的一条路径。
        if not src.is_dir():
            return False, f"旧目录不存在（{src}），无需迁移"

        # ② 新目录已存在 → 用户已用新版跑过，绝不合并覆盖。
        if dst.exists():
            return False, f"新目录已存在（{dst}），保留现状不合并"

        # ③ 防御性：同一路径（理论上不会走到）。
        try:
            if src.resolve() == dst.resolve():
                return False, "新旧目录是同一个，跳过"
        except OSError:
            pass

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return True, f"已迁移 {src} → {dst}"
    except Exception as exc:                       # noqa: BLE001 - 迁移失败不致命
        return False, f"迁移失败（已忽略）：{exc}"
