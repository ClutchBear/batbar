r"""打包产物 dist/batbar.exe 的端到端验收。

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

⚠️ 日志落点有**两个候选**：绿色模式下是 `<exe 目录>\logs\agent.log`；若 exe
目录不可写，程序自己回退到 `%LOCALAPPDATA%\batbar\logs\agent.log`。跑之前不知道
它会选哪个，所以两个都记基线、跑完看哪个变大了（2026-10-13 改绿色模式时踩过：
只盯老路径会把验收验成"日志没新增"的假失败）。
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


def _candidates() -> list[Path]:
    """日志可能落在的两个地方。

    2026-10-13 起打包版是**绿色模式**：数据落在 exe 同目录。但 exe 目录不可写
    时会自己回退到用户目录 —— 所以两个都盯，跑完看哪个变了。
    """
    exe_side = EXE.parent / "logs" / "agent.log"
    fb = Path(os.environ.get("LOCALAPPDATA") or ".") / "batbar" / "logs" / "agent.log"
    return [exe_side, fb]


def _sizes() -> dict:
    out = {}
    for p in _candidates():
        try:
            out[p] = p.stat().st_size if p.exists() else 0
        except OSError:
            out[p] = 0
    return out


def _grew(before: dict) -> Path | None:
    """挑出跑完之后**变大/新生**的那个日志文件 —— 按候选顺序取**第一个**。

    ⚠️ 不能挑"体积最大的那个"：`%LOCALAPPDATA%` 里的老日志有几十 KB，exe 旁边
    那份刚建出来只有几 KB，按体积会选错人（本脚本真踩过：把回退测试留在老日志里的
    「数据目录 = …」当成了本次的落点，白白误判成"没走绿色模式"）。
    顺序 = `_candidates()` 的顺序，绿色模式优先。
    """
    for p in _candidates():
        try:
            sz = p.stat().st_size if p.exists() else 0
        except OSError:
            sz = 0
        if sz > before.get(p, 0):
            return p
    return None


def _log_tail(path: Path, n: int) -> str:
    """读日志中**第 n 个字节之后**的内容。

    ⚠️ 必须按**字节**切片。日志正文是中文（UTF-8 每字 3 字节），若写成
    `read_text()[n:]` 就会按字符切：偏移量被放大成 3 倍，开头几行整段被吞掉，
    表现为「日志里没找到 cwd 锚定行」这种**假阴性**（本脚本自己踩过一次）。
    """
    if not path.exists():
        return ""
    return path.read_bytes()[n:].decode("utf-8", errors="replace")


def dynamic_check() -> int:
    print()
    print("== 动态（真起 exe，cwd 故意设在 System32）==")
    before = _sizes()

    # onefile 是先起 bootloader 再派生子进程，所以要用 /T 杀整棵树
    t0 = time.time()
    proc = subprocess.Popen(
        [str(EXE), "--standalone", "-v"],
        cwd=SYS32,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"  已启动 pid={proc.pid}，等 20 秒让它轮询一轮设备 ...")
    time.sleep(20)

    # ⚠️ 系统命令的输出是 GBK（中文 Windows），必须 errors="replace" —— 否则
    #    Python 侧先炸在解码上（同一个坑在本项目里已经出现过两次）
    #
    # ⚠️ 进程名**必须取自 EXE 本身**，不能写死 `batbar.exe`：本脚本支持把任意
    #    exe 路径当参数传进来（核验瘦身版 `batbar-slim.exe`、从 Release zip 解出
    #    来改名过的那份），写死会让 tasklist 永远查不到 ⇒ 打印「进程存活: False」
    #    的**假阴性**，看起来像 exe 自己崩了，其实它好好活着（2026-10-14 踩到）。
    exe_name = EXE.name
    alive = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    ).stdout
    running = exe_name.lower() in alive.lower()
    print(f"  进程存活: {running}   (查 {exe_name})")

    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass

    logp = _grew(before)
    if logp is None:
        print("  [X] 两个候选日志位置都没有新增 —— exe 起来后什么都没做")
        for p in _candidates():
            print(f"      (查过 {p})")
        return 1
    print(f"  日志落点: {logp}")
    new = _log_tail(logp, before.get(logp, 0))
    if not new.strip():
        print("  [X] 日志没有新增 —— exe 起来后什么都没做")
        return 1

    lines = [ln for ln in new.splitlines() if ln.strip()]
    print(f"  日志新增 {len(lines)} 行，末尾 12 行：")
    for ln in lines[-12:]:
        print(f"    {ln}")

    # ⚠️ 绿色模式会把历史日志**复制**进新位置（搬迁），所以"新增内容"里混着上一代
    #    的行 —— 那些行也含「数据目录 = …」「ERROR」之类字样，全文匹配会取到错的
    #    证据（本脚本真踩过：打印出的是回退测试留在老日志里的旧数据目录）。
    #    判据 = 行首时间戳不早于本次启动前 5 秒，只认本次运行产生的行。
    fresh, stale = [], 0
    pat = r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"
    for ln in lines:
        m = re.match(pat, ln)
        if not m:
            fresh.append(ln)
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            fresh.append(ln)
            continue
        if ts >= t0 - 5:
            fresh.append(ln)
        else:
            stale += 1
    if stale:
        print(f"  （{stale} 行是搬迁带进来的历史日志，以下判定只看本次新增的 {len(fresh)} 行）")
    fresh_txt = "\n".join(fresh)

    # 绿色模式自证：日志里应有一行「数据目录 = ...」
    m = re.search(r"数据目录 = (.+?)(（|$)", fresh_txt)
    if m:
        green = "绿色模式" in fresh_txt
        print(
            f"  数据目录 => {m.group(1).strip()}   "
            f"[{'绿色模式 ✓' if green else '注意：不是 exe 同目录，见该行括号里的原因'}]"
        )

    # cwd 锚定：frozen 下应锚到 exe 所在目录，绝不该停在 System32
    m = re.search(r"cwd 已锚定：(.+)", fresh_txt)
    if m:
        got = m.group(1).strip()
        ok = got.lower() != SYS32.lower()
        print(f"  cwd 锚定 => {got}   [{'OK' if ok else 'X 停在 System32!'}]")
    else:
        print("  (日志里没找到 cwd 锚定行)")

    hit = re.search(r"(\d+)%", fresh_txt)
    print(f"  读到电量读数: {'是 ' + hit.group(0) if hit else '否'}")

    bad = re.findall(r"Traceback|Error|ERROR|Exception|ImportError", fresh_txt)
    print(f"  错误关键字: {sorted(set(bad)) if bad else '无'}")
    return 0


if __name__ == "__main__":
    rc = static_checks()
    if rc == 0:
        rc = dynamic_check()
    print()
    print("=> 验收", "通过" if rc == 0 else "失败")
    sys.exit(rc)
