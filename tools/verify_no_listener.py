"""真机验收：双击 exe 之后，不该有任何监听端口（防火墙弹窗的根因）。

为什么单独写这个脚本：防火墙提示**每台机器通常只弹一次**，在本机点过「取消」
之后就再也复现不出来了 ⇒ 「我这边没看到」等于零证据。所以判据必须是客观可
复现的：**进程起来后，枚举它的 TCP 监听，结果必须为空**。

而且必须带**阳性对照**：先证明这套枚举方法确实抓得到监听（自己开一个再关掉），
否则「枚举结果 0 条」可能只是方法失效（跟黑窗 bug 那次一样，光报 0 没说服力）。

用法：
    python tools/verify_no_listener.py [path/to/batbar.exe]
"""
from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _netstat_lines() -> list[str]:
    out = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True, text=True, errors="replace",
    ).stdout
    return out.splitlines()


def listeners_of(pid: int) -> list[str]:
    """该 PID 名下所有 LISTENING 的 TCP 端点。"""
    res = []
    for ln in _netstat_lines():
        parts = ln.split()
        # TCP   0.0.0.0:51788   0.0.0.0:0   LISTENING   12345
        if len(parts) >= 5 and parts[0].upper() == "TCP" \
                and parts[3].upper() == "LISTENING" and parts[-1] == str(pid):
            res.append(parts[1])
    return res


def positive_control() -> bool:
    """阳性对照：本方法能不能抓到一个真实存在的监听？"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    pid = __import__("os").getpid()
    try:
        found = listeners_of(pid)
        ok = any(str(port) in a for a in found)
        print("    阳性对照：本进程自己在 127.0.0.1:%d 开了监听" % port)
        print("             枚举结果 =", found or "(空)")
        print("             =>", "抓到了，方法有效 ✓" if ok else
              "** 没抓到，枚举方法失效，下面的 0 条不可信 **")
        return ok
    finally:
        srv.close()


def main() -> int:
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist/batbar.exe")
    if not exe.is_file():
        print("找不到 exe:", exe)
        return 2

    print("=" * 66)
    print("验收：%s" % exe)
    print("体积：%s B" % format(exe.stat().st_size, ","))
    print("=" * 66)

    print("\n[0] 先证明枚举方法本身有效")
    if not positive_control():
        return 3

    stage = Path(tempfile.mkdtemp(prefix="batbar-listen-")) / "pkg"
    stage.mkdir(parents=True)
    target = stage / "batbar.exe"
    target.write_bytes(exe.read_bytes())       # 绿色模式 ⇒ 数据跟着 exe 走

    print("\n[1] 启动 exe（干净目录 %s）" % stage)
    proc = subprocess.Popen([str(target), "--standalone"])
    try:
        deadline = time.time() + 45
        while time.time() < deadline and not (stage / "agent.pid").exists():
            if proc.poll() is not None:
                print("    exe 提前退出，code =", proc.returncode)
                break
            time.sleep(0.5)
        # 起来之后再给它点时间把端口开出来（监听是 run() 早期就做的）
        time.sleep(12)

        if proc.poll() is not None:
            print("    ** exe 没活着，无法判定 **")
            return 4

        print("    运行中，PID =", proc.pid)
        hits = listeners_of(proc.pid)
        print("    该 PID 的监听端点：", hits or "（无）")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n" + "=" * 66)
    if hits:
        print("** 仍有监听端口 ⇒ 防火墙还会弹窗 **")
        return 1
    print("结论：零监听端口 ⇒ 不会再触发防火墙提示 ✓")
    print("      （batbar 读电量走本地 HID，全程不联网）")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
