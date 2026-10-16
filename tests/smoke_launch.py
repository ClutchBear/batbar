# -*- coding: utf-8 -*-
"""黑盒冒烟测试：真启动 agent，验「真实产物」能跑通。

与 382 项 mock 单测的区别（这正是它的价值所在）：
  * mock 测试跑在**测试进程内**，import 走 sys.path 注入，从不真的起托盘、
    从不真的 load hidapi DLL、从不真的建 Win32 窗口 —— 所以「import 链断裂 /
    venv 缺依赖 / ctypes 签名写错 / 单实例互斥量抢不到 / boot 卡在某一步」
    这类**只有真跑才会暴露**的问题，382 项一条都测不出来。
  * 本测试**真 spawn 一个 python 子进程**跑 `agent.py --standalone`，
    读它写出的**真实日志**（不是 mock 的 log 对象）来判定启动是否走完。
    这是最接近「用户双击 exe」的黑盒验证。

设计要点（都踩过坑）：
  1. **绝不碰用户的活 agent**：用 `--allow-multiple` 跳过单实例互斥量
     （否则测试实例会被用户正在跑的 agent 挡下，报错退出）；再把日志目录
     用 BATBAR_LOG_DIR 重定向到临时目录（不污染 %LOCALAPPDATA%）。
  2. **托盘不进真实任务栏**：--standalone 会建托盘，但测试实例有自己的
     NIM_ADD；跑完立刻 kill，图标随进程消失。加了 --allow-multiple 后
     与活实例井水不犯河水（各自独立的 HID 句柄；只是会短暂抢一下输入队列，
     对本测试无影响 —— 我们不断言电量）。
  3. **判定靠日志阶段行**：`boot: <stage> +Ns` 是 agent 自己打的启动打点，
     比「等 N 秒看图标在不在」稳得多。任何一步缺失 = 启动链断了。
  4. **时间预算**：首次蓝牙 CIM 查询实测 4~25s，所以给宽松的 60s 上限；
     只对「NIM_ADD 之前」的阶段做相对时间断言（这些步不碰硬件）。

用法：
    .venv/Scripts/python.exe tests/smoke_launch.py           # 完整冒烟
    .venv/Scripts/python.exe tests/smoke_launch.py --quick   # 只到「Agent 构造完成」

退出码：0 = 通过，1 = 失败（失败原因打 stdout）。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT = ROOT / "src" / "batbar" / "agent.py"

# 启动序列里「不碰硬件」的阶段（NIM_ADD 之前都该在 1s 内出现）。
# 真机实测基线（probes/relaunch_agent.py）：日志就绪 +0.03s / 单实例 +0.05s
# / Agent 构造 +0.10s / 托盘首帧 +0.21s / NIM_ADD +0.24s。
EARLY_STAGES = ["日志就绪", "单实例锁已占", "Agent 构造完成", "托盘首帧已请求"]
LATE_STAGE = "图标已注册（NIM_ADD）"

# 阶段行形如：  boot: 日志就绪              +0.03s
# 注意阶段名里**含空格**（如「Agent 构造完成」「图标已注册（NIM_ADD）内容=…」），
# 所以不能 \S+，要贪到末尾的 +Ns 之前，再 strip。
_STAGE_RE = re.compile(r"boot:\s*(?P<stage>.+?)\s*\+\s*(?P<sec>[\d.]+)s")

HARD_TIMEOUT_S = 60.0  # 最坏情况（首次蓝牙 CIM 25s）也够
EARLY_BUDGET_S = 2.0  # 前面几步不该超过这个时间（相对 _BOOT_T0）


def _fail(msg: str) -> None:
    print(f"\n[FAIL] {msg}")
    raise SystemExit(1)


def _read_log(path: pathlib.Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _parse_stages(text: str) -> dict[str, float]:
    """从日志里抽出 {阶段名: 秒}。同名只留第一次（首次启动各阶段只打一次）。"""
    out: dict[str, float] = {}
    for m in _STAGE_RE.finditer(text):
        stage = m.group("stage").strip()
        if stage not in out:
            out[stage] = float(m.group("sec"))
    return out


def _find_stage(stages: dict[str, float], key: str) -> float | None:
    """按前缀找阶段 —— NIM_ADD 那行后面还挂着 `内容='…' 等待=…`，得模糊匹配。"""
    if key in stages:
        return stages[key]
    for name, sec in stages.items():
        if name.startswith(key):
            return sec
    return None


def _assert_no_orphan(pid: int) -> None:
    """收尾复核：确认刚才那个子进程真没了（WorkBuddy 环境铁律：删/杀完必复核）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).stdout.decode("utf-8", "replace")
    except Exception:
        return  # 复核本身失败不阻塞（非关键路径）
    if str(pid) in out:
        print(f"[warn] 冒烟子进程 {pid} 仍在运行，请手动确认")


def run_smoke(quick: bool = False) -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="batbar-smoke-"))
    log_dir = tmp / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "agent.log"

    env = dict(os.environ)
    env["BATBAR_LOG_DIR"] = str(log_dir)  # 日志 + pid 都重定向到临时目录
    env["BATBAR_SINGLETON_POPUP"] = "0"  # 不弹窗（无人值守）
    env["BATBAR_DEBUG"] = "1"  # 开 DEBUG，日志更全（boot 行照样有）
    # 兜底：即使 venv 没做 editable install，也能 import 到包
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")

    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        _fail(f"找不到项目 venv 解释器：{py}（先跑 setup-agent.cmd 建环境）")

    # ⚠️ 必须 `-m batbar.agent`，不能直接传 agent.py 路径 —— 后者会让
    # `from . import xxx` 报 "attempted relative import with no known parent
    # package"（agent.py 是包内模块，不是脚本）。这个坑正是本冒烟测试第一次
    # 跑就抓到的真实问题，见 tests/smoke_launch.py 的历史。
    cmd = [str(py), "-m", "batbar.agent", "--standalone", "--allow-multiple", "-v"]
    print(f"冒烟启动：{py.name} -m batbar.agent --standalone --allow-multiple")
    print(f"  日志重定向 → {log_file}")
    print(f"  模式：{'quick（只到 Agent 构造）' if quick else 'full（等到 NIM_ADD）'}\n")

    t0 = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    stages: dict[str, float] = {}
    need = list(EARLY_STAGES) + ([] if quick else [LATE_STAGE])
    ok = False
    try:
        while time.perf_counter() - t0 < HARD_TIMEOUT_S:
            if proc.poll() is not None:
                # 子进程提前退了（import 炸 / 参数错 / 异常）
                out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
                log = _read_log(log_file)
                _fail(
                    f"agent 提前退出（exit={proc.returncode}）\n"
                    f"--- stdout/stderr ---\n{out[-3000:]}\n"
                    f"--- agent.log ---\n{log[-3000:]}"
                )
            stages = _parse_stages(_read_log(log_file))
            if all(_find_stage(stages, s) is not None for s in need):
                ok = True
                break
            time.sleep(0.1)
    finally:
        # 无论如何都要收掉子进程树，绝不留孤儿 agent。
        # 先 terminate（给 agent 自己跑托盘清理的机会），再 taskkill /T 兜底
        # （Windows 上 subprocess.kill 只杀直接子进程，agent 若 spawn 了子线程
        # 转成的进程会漏网；/T 连整棵树一起收，/F 强制）。
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        if proc.poll() is None:  # 还活着 → 强杀进程树
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except Exception:
                pass
        _assert_no_orphan(proc.pid)

    print("== 启动阶段时间线（agent 自报 +Ns，相对它自己的 _BOOT_T0）==")
    for s in need:
        got = _find_stage(stages, s)
        flag = "  " if got is not None else "❌"
        print(f"  {flag} {s:<18} {'+%.2fs' % got if got is not None else '<缺失>'}")

    if not ok:
        log = _read_log(log_file)
        _fail(
            "启动未走完，缺阶段："
            + ", ".join(s for s in need if _find_stage(stages, s) is None)
            + f"\n--- agent.log 末尾 ---\n{log[-3000:]}"
        )

    # 早期阶段的时间预算（这些步不碰硬件，不该慢）
    for s in EARLY_STAGES:
        sec = _find_stage(stages, s)
        if sec is not None and sec > EARLY_BUDGET_S:
            _fail(f"阶段「{s}」耗时 {sec:.2f}s 超预算 {EARLY_BUDGET_S}s（不碰硬件的步不该慢）")

    # 进程确实活着（能 kill 说明它一直在跑）
    print(f"\n[ok] 启动链完整走通（耗时 {time.perf_counter() - t0:.2f}s）")

    # 额外黑盒断言：托盘 NIM_ADD 那行的内容不是空白
    log = _read_log(log_file)
    m = re.search(r"图标已注册（NIM_ADD）内容=(?P<content>.+?) 等待=", log)
    if m:
        print(f"[ok] NIM_ADD 首帧内容 = {m.group('content')}")
    else:
        print("[warn] 没解析到 NIM_ADD 内容字段（日志格式可能变了）")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n=== 冒烟测试通过 ===")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="batbar 黑盒冒烟测试")
    ap.add_argument(
        "--quick", action="store_true", help="只验「进程能起来 + 构造函数不炸」，不等托盘 NIM_ADD"
    )
    args = ap.parse_args()
    return run_smoke(quick=args.quick)


if __name__ == "__main__":
    raise SystemExit(main())
