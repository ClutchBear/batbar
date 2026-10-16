"""现场取证（bug 12）：最后一台蓝牙设备关闭后，底层查询链各环节返回什么。

只读探测，不改任何状态。跑法：
  .venv\\Scripts\\python.exe probes\\probe_bt_last_device.py
"""

import json
import subprocess
import sys

import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from batbar import bt_battery as B  # noqa: E402

shell = B._pwsh()
print("shell:", shell)

# 1) 原始电量表（不过滤）——空表 = bug 12 触发条件（整表空 → 宽限 180s）
r = subprocess.run(
    [shell, "-NoProfile", "-Command", B._PS_QUERY],
    capture_output=True,
    text=True,
    timeout=30,
    encoding="utf-8",
    errors="replace",
)
print("PS_QUERY raw:", (r.stdout or "").strip()[:800])

# 2) WinRT 连接态（键盘 MAC，权威源）
kb_mac = "E33312B589DA"  # LEOBOG Hi98C Pro
script = B._PS_CONN.replace("__MACS__", "@('%s')" % kb_mac)
r2 = subprocess.run(
    ["powershell", "-NoProfile", "-Command", script],
    capture_output=True,
    text=True,
    timeout=30,
    encoding="utf-8",
    errors="replace",
)
print("WinRT conn:", (r2.stdout or "").strip()[:200])

# 3) 活跃 HID 派生节点（BLE HID 键盘的第二判据）
print("hid_live_macs:", sorted(B.hid_live_macs()))
