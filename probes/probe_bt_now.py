# -*- coding: utf-8 -*-
"""实探：蓝牙电量链路此刻到底返回什么（LEOBOG BT 开着 / 耳机状态）。

只读查询（Get-CimInstance / WinRT 连接态），不碰 HID 设备，不影响托盘进程。
"""

import json
import subprocess
import sys

sys.path.insert(0, "src")
from batbar import bt_battery as B  # noqa: E402


def main() -> int:
    shell = B._pwsh()
    print("[i] shell =", shell)
    r = subprocess.run(
        [shell, "-NoProfile", "-Command", B._PS_QUERY],
        capture_output=True,
        text=True,
        timeout=55,
        encoding="utf-8",
        errors="replace",
    )
    out = (r.stdout or "").strip()
    print("[i] raw out head:", (out[:500] + "...") if len(out) > 500 else (out or "(EMPTY)"))
    if r.stderr.strip():
        print("[i] stderr head:", r.stderr.strip()[:200])
    data = json.loads(out) if out and out != "null" else []
    if isinstance(data, dict):
        data = [data]
    print("[i] candidate rows:")
    for d in data or []:
        print("   ", d.get("name"), "| mac=", d.get("mac"), "| bat=", d.get("battery"))
    macs = sorted({str(d.get("mac") or "").upper() for d in data if isinstance(d, dict) and d.get("mac")})
    print("[i] macs:", macs)
    conn = B.connected_macs(macs)
    print("[i] connected:", conn)
    print("[i] parsed (未连接剔除后):", B._parse(data, connected=conn or None))
    live = B.hid_live_macs()
    print("[i] hid_live_macs:", sorted(live))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
