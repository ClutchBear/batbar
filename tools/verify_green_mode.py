# -*- coding: utf-8 -*-
"""绿色模式端到端验收：把 exe 拷到一个**干净目录**跑，确认数据真的落在它旁边。

验的六件事（每一件都对应一个真实坏结局）：
  1. `<exe目录>\\logs\\agent.log` 真的被创建 —— 否则"绿色"就是空话
  2. `<exe目录>\\last-known.json` 出现 —— 电量表跟着 exe 走
  3. `<exe目录>\\agent.pid` 出现 —— PID 也跟着走
  4. 日志里有一行「数据目录 = …（绿色模式：跟着 exe 走）」
  5. **历史数据被复制进来了**（`%LOCALAPPDATA%\\batbar` / `\\logi-tray` → 新目录），
     且**旧目录仍然存在**（用户要求：只复制不删源）
  6. 全程 `%LOCALAPPDATA%\\batbar\\logs\\agent.log` **不再增长** —— 证明没偷偷
     继续往用户目录写

用法: python verify_green_mode.py <exe路径> [秒数]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL = Path(os.environ.get("LOCALAPPDATA") or ".")
LEGACY = LOCAL / "batbar"
LEGACY_OLD = LOCAL / "logi-tray"

EXE_SRC = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\workbuddy\space\batbar\dist\batbar.exe").resolve()
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0


def _tree(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def main() -> int:
    if not EXE_SRC.is_file():
        print("[X] 找不到 exe:", EXE_SRC)
        return 1

    work = Path(tempfile.mkdtemp(prefix="batbar-green-"))
    exe = work / "batbar.exe"  # ★ 平铺：exe 直接躺在工作目录里
    shutil.copy2(EXE_SRC, exe)

    legacy_log = LEGACY / "logs" / "agent.log"
    before = {
        "legacy_files": _tree(LEGACY),
        "legacy_old_files": _tree(LEGACY_OLD),
        "legacy_log_size": _size(legacy_log),
        "work_files": _tree(work),
    }

    print("工作目录:", work)
    print(
        "跑之前 —— 用户目录里有 %d 个文件，历史目录里有 %d 个文件"
        % (len(before["legacy_files"]), len(before["legacy_old_files"]))
    )

    proc = subprocess.Popen(
        [str(exe)],
        cwd=r"C:\Windows\System32",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(SECONDS)
    alive = proc.poll() is None
    print(f"  进程存活: {alive}  (pid={proc.pid})")

    after_files = _tree(work)
    log = work / "logs" / "agent.log"
    log_txt = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""

    checks = {
        "exe 同目录出现 logs/agent.log": log.is_file(),
        "exe 同目录出现 last-known.json": (work / "last-known.json").is_file(),
        "exe 同目录出现 agent.pid": (work / "agent.pid").is_file(),
        "日志声明绿色模式": "绿色模式" in log_txt,
        "日志声明数据目录": "数据目录 =" in log_txt,
        "历史数据被复制进来": any(
            f.startswith("logs/") or f == "last-known.json" for f in after_files if f != "logs/agent.log"
        ),
        "旧目录仍在（只复制不删源）": bool(_tree(LEGACY)) or not before["legacy_files"],
        "用户目录日志未再增长": _size(legacy_log) <= before["legacy_log_size"],
    }

    print("\n== 结果 ==")
    for k, v in checks.items():
        print(f"  [{'OK' if v else 'X '}] {k}")
    print("\n工作目录内容:", after_files)
    print("用户目录内容:", _tree(LEGACY))
    print("历史目录内容:", _tree(LEGACY_OLD))
    if log_txt:
        print("\n日志头 8 行：")
        for ln in log_txt.splitlines()[:8]:
            print("   ", ln[:160])

    # 收尸：杀掉这次起的进程树（别动别的 batbar 实例）
    subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
        capture_output=True,
        check=False,
        creationflags=NO_WINDOW,
    )

    with open(
        Path(__file__).resolve().parents[1] / "_tmp" / "verify_green_result.json", "w", encoding="utf-8"
    ) as fh:
        json.dump(
            {
                "work": str(work),
                "files": after_files,
                "checks": checks,
                "log_head": log_txt.splitlines()[:20],
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    bad = [k for k, v in checks.items() if not v]
    print("\n判定:", "全部通过" if not bad else "未通过：" + "、".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
