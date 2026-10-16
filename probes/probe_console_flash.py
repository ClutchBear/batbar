"""Black-box probe: does spawning a console child from a *GUI* process pop a window?

Must be launched with pythonw.exe (no console) -- with a console present the
child just inherits it and no window is created, which is why this never
reproduces when running `python agent.py` from a terminal.

Writes _tmp/flash_result.json and exits.
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flash_result.json")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def shell_process_running():
    """tasklist 里有没有 pwsh / powershell —— 用来证明子进程**真的起来了**。

    调用 tasklist 自己也要带 CREATE_NO_WINDOW：本脚本用 pythonw 跑（无控制台），
    否则每采样一次就给自己弹一个窗，把观测污染掉。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError:
        return None
    text = (out.stdout or "").lower()
    return "pwsh.exe" in text or "powershell.exe" in text


u32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def snapshot():
    """{hwnd: (class, title)} of all *visible* top-level windows."""
    found = {}

    def cb(hwnd, _lp):
        try:
            if not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            tbuf = ctypes.create_unicode_buffer(n + 2)
            u32.GetWindowTextW(hwnd, tbuf, n + 2)
            cbuf = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cbuf, 256)
            found[int(hwnd)] = [cbuf.value, tbuf.value]
        except Exception:  # noqa: BLE001 - probing must never die
            pass
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def probe(mode, extra_kwargs):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    before = snapshot()
    kwargs = dict(
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
    )
    kwargs.update(extra_kwargs)
    proc = subprocess.Popen([shell, "-NoProfile", "-Command", "Start-Sleep -Milliseconds 2600"], **kwargs)
    seen = {}
    shell_seen = False
    for _ in range(5):  # 采样 5 轮，覆盖 MSIX 激活的慢路径
        time.sleep(0.7)
        for h, v in snapshot().items():
            if h not in before:
                seen.setdefault(h, v)
        if shell_process_running():
            shell_seen = True
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(0.6)
    new = seen
    return {
        "mode": mode,
        "shell": shell,
        "has_own_console": bool(ctypes.windll.kernel32.GetConsoleWindow()),
        "shell_process_seen": shell_seen,
        "new_windows": [{"hwnd": h, "class": v[0], "title": v[1]} for h, v in new.items()],
    }


def main():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    hidden = {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": si,
    }

    results = [
        probe("naked (no flags)", {}),
        probe("CREATE_NO_WINDOW + SW_HIDE", hidden),
        probe("naked again (confirm reproducible)", {}),
    ]
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump({"error": "%s: %s" % (type(exc).__name__, exc)}, fh, ensure_ascii=False, indent=2)
        raise SystemExit(1)
