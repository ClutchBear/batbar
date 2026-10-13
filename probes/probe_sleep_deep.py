"""睡眠判定实测：先让鼠标静置 ~40s 入睡，再用「短枪 / 长枪 / 寄存器 0x02」分别探。

回答两个问题：
  ① 睡眠中的 G304，长枪（2.5s×2）能不能叫醒？（能 ⇒ 「长枪不应答」= 关机）
  ② 寄存器 0x02 在鼠标睡/关机时到底报什么？（若一直报 1 ⇒ 它报的是对码数，不能用）
只读，不写设备。运行前需停掉托盘 agent（抢接收器输入队列）。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

from batbar import hidpp as H  # noqa: E402
from batbar import transport as T  # noqa: E402
from batbar.session import HidppSession  # noqa: E402

IDLE_S = 40


def build():
    cands = T.find_devices()
    handles = [(d, T.open_path(d["path"])) for d in cands]
    primary_i, flen = -1, None
    for i, (_d, dev) in enumerate(handles):
        n = T.try_write(dev, H.build_write_frame(H.DEV_RECEIVER, bytes([0x00, 0x10, 0x00])))
        if n is not None:
            primary_i, flen = i, n
            break
    pd, pdev = handles[primary_i]
    readers = [dev for i, (_d, dev) in enumerate(handles) if i != primary_i]
    tr = T.HidapiTransport(pdev, frame_len=flen, readers=readers)
    probe = HidppSession(tr, device_index=H.DEV_RECEIVER, timeout=0.8, retries=1)
    idx = None
    for slot in range(1, 8):
        probe._devnum = slot
        probe._feat_cache.clear()
        probe._battery_feat = None
        if probe.battery_feature() is not None:
            idx = slot
            break
    print(f"接收器 {pd.get('product_string')}  对码槽位 = {idx}")
    return tr, idx


tr, slot = build()
s = HidppSession(tr, device_index=slot or H.DEV_RECEIVER)


def read(label: str, timeout: float, retries: int):
    s._feat_cache.clear()
    s._battery_feat = None
    s._timeout, s._retries = timeout, retries
    t0 = time.monotonic()
    info = s.read_battery()
    dt = time.monotonic() - t0
    print(f"  {label:<22} → {'%d%%' % info.percent if info and info.percent is not None else '不应答'}"
          f"   {dt:.2f}s")
    return info


print("\n[1] 先敲一下（唤醒），确认链路正常：")
read("短枪 0.6s×2", 0.6, 1)

print(f"\n[2] 静置 {IDLE_S}s 让它入睡（期间不要碰鼠标/触摸板）...")
for left in range(IDLE_S, 0, -5):
    print(f"    还剩 {left}s", end="\r")
    time.sleep(5)
print("\n[3] 静置结束，开始探测：")
read("短枪 0.6s×2", 0.6, 1)
read("长枪 2.5s×2", 2.5, 1)
print(f"  寄存器 0x02 已连接数 = {s.connection_state()}")

print("\n[4] 再静置 20s 后复测（排除第一次唤醒的残留）：")
time.sleep(20)
read("短枪 0.6s×2", 0.6, 1)
read("长枪 2.5s×2", 2.5, 1)
print(f"  寄存器 0x02 已连接数 = {s.connection_state()}")
