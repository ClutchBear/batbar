# -*- coding: utf-8 -*-
"""bug 15 真机验证：sync_now 双向快判的耗时与准确性。

当前键盘/耳机应在连（用户刚验收过）。本探针：
1) 先跑一次 _run_query 建立 _mac_by_name 映射（模拟 agent 常驻后已知的设备）
2) 把一台在连设备从 _devices 里"藏起来"（模拟它之前断开被摘了）
3) 调 sync_now，看它能否靠 WinRT 秒判"上线"并加回来 + 计时
"""
import time

from batbar import bt_battery as B

b = B.BluetoothBattery()

# ① 建立真实映射 + 设备列表（CIM 全量查询，~2-4s，只做一次）
t0 = time.perf_counter()
b._run_query(force_conn=True)
t1 = time.perf_counter()
print("初始 _run_query: %.2fs" % (t1 - t0))
print("  _devices     :", b._devices)
print("  _mac_by_name :", b._mac_by_name)

if not b._mac_by_name:
    print("\n[跳过] 没有蓝牙设备映射，无法验证上线快判")
    raise SystemExit(0)

# ② 挑一台在连设备，从 _devices 藏起来（模拟"之前断开被摘"）
victim = next(iter(b._devices))
saved = b._devices.pop(victim)
print("\n模拟下线：把 %r 从列表摘掉（当前列表=%s）" % (victim, sorted(b._devices)))

# ③ sync_now 应该靠 WinRT 把它秒判"上线"加回来
t2 = time.perf_counter()
added, dropped = b.sync_now()
t3 = time.perf_counter()
print("sync_now: %.2fs -> added=%s dropped=%s" % (t3 - t2, added, dropped))
print("  _devices 恢复:", b._devices)
print("  闪回保护标记 :", b._fast_add_protect)

if victim in added:
    print("\n✅ 上线快判生效：%r 被 WinRT 秒判上线并加回（耗时 %.2fs）" % (victim, t3 - t2))
else:
    print("\n⚠️ %r 未被加回——检查它当前是否真在连" % victim)
