"""启动首轮各阶段耗时实测（只读，不写设备）。

回答用户问题：为什么只有一个鼠标时「启动中…」挂得久。
分解 _bt_warm_loop 首轮的每一步：
  ① prewarm()         蓝牙 CIM 查询
  ② find_devices()    HID 枚举
  ③ _ensure_device()  建会话（含 _discover_devnum 槽位扫描）
  ④ 单个空槽位的探测预算（7 槽全空 = 7 × 这个数）
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

from batbar import transport as T  # noqa: E402
from batbar import hidpp as H  # noqa: E402
from batbar.agent import Agent  # noqa: E402
from batbar.bt_battery import BluetoothBattery  # noqa: E402


def t(label: str, fn):
    t0 = time.monotonic()
    try:
        r = fn()
    except Exception as exc:  # noqa: BLE001
        r = f"<{type(exc).__name__}: {exc}>"
    dt = time.monotonic() - t0
    print(f"{label:<34} {dt:6.2f}s   {r}")
    return dt, r


print("=== ① 蓝牙预热 ===")
bt = BluetoothBattery()
t("BluetoothBattery().prewarm()", bt.prewarm)
t("cached_devices()", lambda: bt.cached_devices())

print("\n=== ② HID 枚举 ===")
_dt, cands = t("T.find_devices()", T.find_devices)
print(f"    候选 collection: {len(cands) if isinstance(cands, list) else cands}")

print("\n=== ③ 建会话（含槽位扫描）===")
a = Agent()
_dt, sess = t("Agent()._ensure_device()", a._ensure_device)
devnum = getattr(sess, "_devnum", None)
print(f"    device_index = {devnum} (0xFF=接收器本体, 1-7=对码槽位)")

print("\n=== ④ 空槽位探测预算（全空扫描 = 7 × 此值）===")
if sess is not None:
    for idx in (7, 6):
        t0 = time.monotonic()
        sess._devnum = idx
        sess._feat_cache.clear()
        sess._battery_feat = None
        try:
            hit = sess.battery_feature() is not None
        except Exception as exc:  # noqa: BLE001
            hit = f"<{type(exc).__name__}>"
        print(
            f"    槽位 {idx} 探测{'命中' if hit is True else '无应答'}          "
            f"{time.monotonic() - t0:6.2f}s   (0.8s × 2 枪)"
        )

print("\n=== ⑤ 接收器寄存器 0x02（新增的冷启动加速判据）===")
if sess is not None:
    t0 = time.monotonic()
    cnt = sess.connection_state()
    print(f"    connection_state() = {cnt}          {time.monotonic() - t0:6.2f}s")
