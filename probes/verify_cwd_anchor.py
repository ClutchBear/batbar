# -*- coding: utf-8 -*-
"""真机验证 cwd 锚定（2026-10-13）。

模拟真实故障场景：进程 cwd 被外部入口设成 C:\\Windows\\System32
（注册表 Run 键 / 计划任务 / restart-diag.bat 的常见行为），
看 anchor_cwd() 是否真的把工作目录搬走。

用 subprocess 起独立进程，避免污染本进程 cwd。
注意：子进程用 `python -c` 执行，**没有 `__file__`**，
所以 src 路径由父进程算好后经环境变量 `BATBAR_SRC` 传入。
"""
from __future__ import annotations

import os
import subprocess
import sys

SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

CHILD = r'''
import os, sys
os.chdir(r"C:\Windows\System32")
print("BEFORE:", os.getcwd(), flush=True)
sys.path.insert(0, os.environ["BATBAR_SRC"])
from batbar import agent as A
got = A.anchor_cwd()
print("AFTER :", os.getcwd(), flush=True)
print("RET   :", got, flush=True)
print("BASE  :", sys.base_prefix, flush=True)
print("MATCH :", os.getcwd() == sys.base_prefix, flush=True)
'''

p = subprocess.run(
    [sys.executable, "-c", CHILD],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    cwd=r"C:\Windows\System32",
    env={**os.environ, "BATBAR_SRC": SRC},
)
out = (p.stdout or "").strip()
err = (p.stderr or "").strip()
print(out)
if err:
    print("--- stderr ---")
    print(err[-800:])

ok = "MATCH : True" in out and "BEFORE: C:\\Windows\\System32" in out
print()
print("=== 验证" + ("通过" if ok else "失败") + " ===")
raise SystemExit(0 if ok else 1)
