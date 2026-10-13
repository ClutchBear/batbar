# -*- coding: utf-8 -*-
"""绿色模式的**回退**验证：exe 放在不可写目录时，数据必须退到用户目录。

为什么值得单独验：绿色模式最大的风险就是用户把 exe 解到 `C:\\Program Files\\`
或只读盘 —— 如果那时程序**写不了就干脆不落盘**，你会得到一个没有日志的托盘程序，
出问题只能瞎猜。所以"不可写 ⇒ 回退 %LOCALAPPDATA%\\batbar"是硬承诺。

做法（真机、真 exe）：
  1. 在临时目录放一份 exe；
  2. `icacls` 给该目录**拒绝写权限**（`/deny <用户>:(WD,AD)`），并用 Python 试写
     一个文件确认真的写不进去（不然测试自己就是假绿）；
  3. 跑 exe，断言：该目录里**没有** `logs\\`；用户目录的日志**有新增**，
     且里面写着「exe 所在目录不可写 … ⇒ 回退到用户目录」；
  4. 无论成败都撤销 ACL 并删掉临时目录（不留后患）。

用法: python tools/verify_green_fallback.py <exe路径> [秒数]
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
LEGACY_LOG = LOCAL / "batbar" / "logs" / "agent.log"
USER = os.environ.get("USERNAME") or ""

EXE_SRC = Path(sys.argv[1] if len(sys.argv) > 1
               else r"D:\workbuddy\space\batbar\dist\batbar.exe").resolve()
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 22.0


def icacls(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["icacls"] + args, capture_output=True, text=True,
                          errors="replace", check=False, creationflags=NO_WINDOW)


def size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def main() -> int:
    if not EXE_SRC.is_file():
        print("[X] 找不到 exe:", EXE_SRC)
        return 1
    if not USER:
        print("[X] 拿不到 USERNAME，无法设 ACL")
        return 1

    work = Path(tempfile.mkdtemp(prefix="batbar-ro-"))
    exe = work / "batbar.exe"
    shutil.copy2(EXE_SRC, exe)

    deny = f"{USER}:(WD,AD)"
    print("只读目录:", work)
    print(icacls([str(work), "/deny", deny]).stdout.strip()[:200])

    # ★ 先自证"真的写不进去"，否则整场测试就是假绿
    try:
        (work / "probe.tmp").write_text("x", encoding="utf-8")
        writable = True
    except OSError as exc:
        writable = False
        print("  自检：写入被拒 ✓（%s）" % type(exc).__name__)

    before_log = size(LEGACY_LOG)
    before_has_logs_dir = (work / "logs").is_dir()

    try:
        if writable:
            print("!! ACL 没生效，本目录仍然可写 —— 这场验证无效")
            return 2

        proc = subprocess.Popen([str(exe)], cwd=r"C:\Windows\System32",
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(SECONDS)
        print("  进程存活:", proc.poll() is None)

        grew = size(LEGACY_LOG) > before_log
        new_txt = ""
        if grew:
            with open(LEGACY_LOG, "rb") as fh:
                fh.seek(before_log)
                new_txt = fh.read().decode("utf-8", errors="replace")

        checks = {
            "只读目录里没有生成 logs/": not before_has_logs_dir and not (work / "logs").is_dir(),
            "只读目录里没有 last-known.json": not (work / "last-known.json").exists(),
            "回退到用户目录并写了日志": grew,
            "日志说明回退原因（不可写）": "不可写" in new_txt,
            "日志声明了最终数据目录": "数据目录 =" in new_txt,
        }
        print("\n== 结果 ==")
        for k, v in checks.items():
            print(f"  [{'OK' if v else 'X '}] {k}")
        for ln in new_txt.splitlines():
            if "数据目录" in ln or "不可写" in ln:
                print("    >", ln[:170])

        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, check=False, creationflags=NO_WINDOW)

        ok = all(checks.values())
        with open(Path(__file__).resolve().parents[1] / "_tmp"
                  / "verify_green_fallback_result.json", "w", encoding="utf-8") as fh:
            json.dump({"work": str(work), "checks": checks,
                       "new_log": new_txt.splitlines()[-12:]}, fh,
                      ensure_ascii=False, indent=2)
        print("\n判定:", "回退行为正确" if ok else "未通过")
        return 0 if ok else 1
    finally:
        # 无论成败都要撤销 ACL，否则临时目录会残留在磁盘上删不掉
        print(icacls([str(work), "/remove:d", USER]).stdout.strip()[:200])
        shutil.rmtree(work, ignore_errors=True)
        print("清理:", "已删除" if not work.exists() else f"!! 残留 {work}")


if __name__ == "__main__":
    sys.exit(main())
