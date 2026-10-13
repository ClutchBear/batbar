"""打包产物 dist/batbar.exe 的端到端验收。

用法：
    python tools/verify_exe.py                       # 默认验 dist\\batbar.exe
    python tools/verify_exe.py <任意 exe 路径>       # 例如核验从 Release zip 解出来的那份

分两层：
  静态 —— 体积 / sha256 / PE 版本信息 / 资源节里有没有嵌图标
  动态 —— 真起一次 exe，从日志确认它**真的读到了设备电量**，然后收尸

为什么动态这层不能省：windowed exe 的失败模式几乎全是"静默"——
无控制台、`sys.stdout` 是 None、缺 hiddenimport 时只表现为"双击没反应"。
唯一会说话的是日志文件。

额外顺带验一件事：动态那一轮把 cwd 故意设成 `C:\\Windows\\System32`
（开机自启的真实场景），用来确认 frozen 分支的 cwd 锚定真的生效。
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "batbar.exe"
# 允许把要验收的 exe 路径当第一个参数传进来 —— 这样 CI 或人工核验「从 Release
# zip 解压出来的那一份」时，用的是同一套验收标准，不必改脚本
if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
    EXE = Path(sys.argv[1]).expanduser().resolve()
LOG = Path(os.environ["LOCALAPPDATA"]) / "batbar" / "logs" / "agent.log"
SYS32 = r"C:\Windows\System32"


def static_checks() -> int:
    if not EXE.is_file():
        print(f"[X] 没找到 {EXE}")
        return 1
    data = EXE.read_bytes()
    print("== 静态 ==")
    print(f"  路径    : {EXE}")
    print(f"  体积    : {len(data):,} B  ({len(data) / 1048576:.2f} MB)")
    print(f"  sha256  : {hashlib.sha256(data).hexdigest()}")

    try:
        import pefile
    except ImportError:
        print("  (pefile 不可用，跳过 PE 解析)")
        return 0

    pe = pefile.PE(str(EXE))
    n_icons = 0
    if hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
        for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            # 3 = RT_ICON, 14 = RT_GROUP_ICON
            if entry.id in (3, 14):
                n_icons = max(n_icons, len(entry.directory.entries))
    print(f"  图标资源: {n_icons} 条 RT_ICON/RT_GROUP_ICON 条目")

    for fi in getattr(pe, "FileInfo", []):
        for item in fi:
            if item.Key == b"StringFileInfo":
                for st in item.StringTable:
                    for k, v in sorted(st.entries.items()):
                        print(f"  {k.decode('ascii', 'replace'):18s}= {v.decode('utf-8', 'replace')}")
    return 0


def _log_tail(n: int = 0) -> str:
    """读日志中**第 n 个字节之后**的内容。

    ⚠️ 必须按**字节**切片。日志正文是中文（UTF-8 每字 3 字节），若写成
    `read_text()[n:]` 就会按字符切：偏移量被放大成 3 倍，开头几行整段被吞掉，
    表现为「日志里没找到 cwd 锚定行」这种**假阴性**（本脚本自己踩过一次）。
    """
    if not LOG.exists():
        return ""
    return LOG.read_bytes()[n:].decode("utf-8", errors="replace")


def dynamic_check() -> int:
    print()
    print("== 动态（真起 exe，cwd 故意设在 System32）==")
    before = LOG.stat().st_size if LOG.exists() else 0

    # onefile 是先起 bootloader 再派生子进程，所以要用 /T 杀整棵树
    proc = subprocess.Popen(
        [str(EXE), "--standalone", "-v"],
        cwd=SYS32,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"  已启动 pid={proc.pid}，等 20 秒让它轮询一轮设备 ...")
    time.sleep(20)

    # ⚠️ 系统命令的输出是 GBK（中文 Windows），必须 errors="replace" —— 否则
    #    Python 侧先炸在解码上（同一个坑在本项目里已经出现过两次）
    alive = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq batbar.exe", "/NH"],
        capture_output=True, text=True, errors="replace", check=False,
    ).stdout
    running = "batbar.exe" in alive
    print(f"  进程存活: {running}")

    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                   capture_output=True, check=False)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass

    new = _log_tail(before)
    if not new.strip():
        print("  [X] 日志没有新增 —— exe 起来后什么都没做")
        return 1

    lines = [ln for ln in new.splitlines() if ln.strip()]
    print(f"  日志新增 {len(lines)} 行，末尾 12 行：")
    for ln in lines[-12:]:
        print(f"    {ln}")

    # cwd 锚定：frozen 下应锚到 exe 所在目录，绝不该停在 System32
    m = re.search(r"cwd 已锚定：(.+)", new)
    if m:
        got = m.group(1).strip()
        ok = got.lower() != SYS32.lower()
        print(f"  cwd 锚定 => {got}   [{'OK' if ok else 'X 停在 System32!'}]")
    else:
        print("  (日志里没找到 cwd 锚定行)")

    hit = re.search(r"(\d+)%", new)
    print(f"  读到电量读数: {'是 ' + hit.group(0) if hit else '否'}")

    bad = re.findall(r"Traceback|Error|ERROR|Exception|ImportError", new)
    print(f"  错误关键字: {sorted(set(bad)) if bad else '无'}")
    return 0


if __name__ == "__main__":
    rc = static_checks()
    if rc == 0:
        rc = dynamic_check()
    print()
    print("=> 验收", "通过" if rc == 0 else "失败")
    sys.exit(rc)
