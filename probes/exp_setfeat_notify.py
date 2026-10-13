"""验证：SetFeature(06 4a...) 是否触发 0xFF02 的 input 通知帧（延迟响应）。"""
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
hid.HidD_SetFeature.restype = ctypes.c_ubyte
hid.HidD_SetFeature.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]

# 0xFF04 feature 口
hits = f24.discover('迈从键盘', 0x05AC, 0x024F)
fpath, flen, frid = hits[0]
# 0xFF02 input 口
cols = pq.enumerate_collections((0x05AC, 0x024F))
ff02 = next((p, n) for (p, u, s, n, f) in cols if u == 0xFF02)

h2 = k32.CreateFileW(ff02[0], 0xC0000000, 3, None, 3, 0x40000000, 0)
r = pq.Reader(h2, report_len=ff02[1])

QUERIES = [
    ('4a@rid6', bytes([0x06, 0x4A, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00])),
    ('87@rid6', bytes([0x06, 0x87, 0x00, 0x00, 0x01, 0x00, 0x02, 0x00])),
]

hf = k32.CreateFileW(fpath, 0xC0000000, 3, None, 3, 0, 0)  # 同步句柄
print(f'feature 口 opened={bool(hf and hf != -1)} len={flen}')

for tag, q in QUERIES:
    # 先清空监听缓冲
    while r.read(10):
        pass
    qb = ctypes.create_string_buffer(q, flen)
    ctypes.set_last_error(0)
    ok = hid.HidD_SetFeature(hf, qb, flen)
    print(f'SetFeature {tag}: {"OK" if ok else "FAIL " + str(ctypes.get_last_error())}')
    t0 = time.time()
    got = 0
    while time.time() - t0 < 8.0:
        d = r.read(60)
        if d:
            got += 1
            print(f'  +{(time.time()-t0)*1000:.0f}ms <- {d.hex(" ")}')
    if not got:
        print('  （8s 零帧）')

k32.CloseHandle(hf)
k32.CloseHandle(h2)
print('DONE')
