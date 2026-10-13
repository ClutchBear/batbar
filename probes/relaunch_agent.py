"""分离启动一个 --standalone agent（用于验证启动耗时），随后打印日志 boot 行。

用法：.venv/Scripts/python.exe probes/relaunch_agent.py [等待秒数]
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = Path.home() / "AppData" / "Local" / "batbar" / "logs" / "agent.log"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def main() -> int:
    wait = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    before = LOG.read_text(encoding="utf-8", errors="replace").count("\n") \
        if LOG.exists() else 0
    p = subprocess.Popen(
        [str(EXE), "-m", "batbar.agent", "--standalone"],
        cwd=str(ROOT),
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True)
    print(f"[i] launched agent pid={p.pid} (detached)")
    time.sleep(wait)
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    print(f"[i] 启动后 {wait:.0f}s 内新增日志：")
    for ln in lines[before:]:
        print("   ", ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
