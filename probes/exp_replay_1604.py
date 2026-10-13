"""逐动作复现 16:04 轮，定位 backlight 通知帧的真实触发者。

16:04 轮动作序列：PRE GetFeature → WRITE 0xFF02 → WRITE 0x000C
→ WRITE 0x0001/0x0080 → WRITE 0xFF04 → (20s 监听) → 收到 1 帧。
"""
import ctypes
import sys
import time

sys.path.insert(0, 'src')
sys.path.insert(0, 'tools')

import probe_query as pq
from batbar import feature24 as f24

k32 = ctypes.windll.kernel32
hid = ctypes.windll.hid
k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p]
hid.HidD_GetFeature.restype = ctypes.c_ubyte
hid.HidD_GetFeature.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]

CMD = bytes(bytearray([0x13, 0x4A, 0x01] + [0x00] * 16)) + bytes([0x5E])

cols = pq.enumerate_collections((0x05AC, 0x024F))
ff02 = next((p, n) for (p, u, s, n, f) in cols if u == 0xFF02)
targets = [(p, u, n) for (p, u, s, n, f) in cols]

h2 = k32.CreateFileW(ff02[0], 0xC0000000, 3, None, 3, 0x40000000, 0)
r = pq.Reader(h2, report_len=ff02[1])


def listen(tag, sec):
    t0 = time.time()
    got = 0
    while time.time() - t0 < sec:
        d = r.read(60)
        if d:
            got += 1
            print(f'  [{tag}] +{(time.time()-t0)*1000:.0f}ms <- {d.hex(" ")}')
    if not got:
        print(f'  [{tag}] {sec}s 零帧')


def try_write(path, tag):
    hw = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, 0)
    if not hw or hw == -1:
        print(f'  [{tag}] write 口打不开 err={ctypes.get_last_error()}')
        return
    rw = pq.Reader(hw, report_len=20)
    res = rw.write(CMD)
    print(f'  [{tag}] write -> {res}')
    k32.CloseHandle(hw)


def try_getfeature():
    hits = f24.discover('迈从键盘', 0x05AC, 0x024F)
    fp, fl, fr = hits[0]
    hf = k32.CreateFileW(fp, 0xC0000000, 3, None, 3, 0, 0)
    b = ctypes.create_string_buffer(fl)
    b[0] = bytes([fr])
    ok = hid.HidD_GetFeature(hf, b, fl)
    print(f'  [GetFeature] -> {"OK " + bytes(b.raw[:fl]).hex(" ") if ok else "FAIL"}')
    k32.CloseHandle(hf)


listen('baseline', 3)
print('== 动作1: PRE GetFeature ==')
try_getfeature()
listen('after-getfeat', 5)

print('== 动作2: WRITE 0xFF02 ==')
try_write(ff02[0], 'FF02')
listen('after-ff02', 5)

for (p, u, n) in targets:
    if u in (0x000C, 0x0001) and n > 0:
        print(f'== 动作3: WRITE up=0x{u:04X} in={n} ==')
        try_write(p, f'up{u:02X}')
        listen(f'after-up{u:02X}', 5)

print('== 动作4: WRITE 0xFF04 ==')
ff04 = next((p for (p, u, s, n, f) in cols if u == 0xFF04), None)
if ff04:
    try_write(ff04, 'FF04')
    listen('after-ff04', 8)

k32.CloseHandle(h2)
print('DONE')
