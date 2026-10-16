"""真机验证：接收器插着但罗技条目不显示时，唤醒预算 + 死会话重扫能否复活。

用法（托盘正在跑时也能跑，只是会有句柄争用风险；建议先退出托盘）：
    .venv/Scripts/python.exe probes/verify_mouse_revive.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from batbar import transport as T  # noqa: E402
from batbar.session import HidppSession, TransportClosed  # noqa: E402
from batbar import hidpp as H  # noqa: E402


def open_session(agent_discover):
    cands = T.find_devices(bt_names=())
    if not cands:
        print("[!] find_devices 空 —— 接收器没插/驱动节点没了")
        return None
    handles = []
    for d in cands:
        try:
            handles.append((d, T.open_path(d["path"])))
        except Exception as exc:
            print(f"    open fail {d.get('path')}: {exc}")
    if not handles:
        print("[!] 所有句柄都打不开")
        return None
    primary_i, flen = -1, None
    for i, (_d, dev) in enumerate(handles):
        n = T.try_write(dev, H.build_write_frame(H.DEV_RECEIVER, bytes([0x00, 0x10, 0x00])))
        if n is not None:
            primary_i, flen = i, n
            break
    if primary_i < 0:
        print("[!] 无 HID++ 可写 collection")
        return None
    pd, pdev = handles[primary_i]
    readers = [dev for i, (_d, dev) in enumerate(handles) if i != primary_i]
    print(
        f"[i] primary up=0x{pd.get('usage_page', 0):X} "
        f"{pd.get('product_string') or pd['path']} write={flen}B readers={len(readers)}"
    )
    idx, _flen = agent_discover(pdev, readers, flen)
    sess = HidppSession(T.HidapiTransport(pdev, frame_len=flen, readers=readers), device_index=idx)
    return sess, idx


def run() -> int:
    from batbar.agent import Agent

    a = object.__new__(Agent)
    a._last_devnum = None

    def discover(pdev, readers, flen):
        return a._discover_devnum(pdev, readers, flen, None)

    res = open_session(discover)
    if res is None:
        return 1
    sess, idx = res
    print(f"[i] 槽位发现结果 device_index={idx} (0xFF={H.DEV_RECEIVER}=接收器本体)")
    print(f"[i] 记忆槽位 _last_devnum={a._last_devnum}")

    for round_no in (1, 2, 3):
        t = time.monotonic()
        try:
            info = sess.read_battery()
        except TransportClosed as exc:
            print(f"[{round_no}] TransportClosed: {exc}")
            return 1
        dt = time.monotonic() - t
        print(f"[{round_no}] read_battery -> {info}  ({dt:.2f}s)")
        if info is not None and info.percent is not None:
            print(f"[OK] 复活：{info.percent}% charging={info.charging}")
            return 0
        time.sleep(0.5)
    print("[!] 三轮都不应答 —— 鼠标关机/深睡，或会话仍是 0xFF 死会话")
    return 1


if __name__ == "__main__":
    sys.exit(run())
