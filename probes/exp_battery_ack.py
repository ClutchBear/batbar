"""决定性实验：迈从 K87 getBattery 命令应答帧捕捉。

官方流程（bundle readCommand wireless 分支逆向）：
  sendReport(0x13, [13 4a 01 00 ... 00 5e])  ← 原始 wirelessCommand+校验和，20B
  → 等 input 帧：commonParser = commandParams([0])==0x4a ? total[1] current[2] data[3..]
  → 拼齐 total 包后解析 batteryLevel
"""
import ctypes
import sys
import time

sys.path.insert(0, 'src')
sys.path.insert(0, 'tools')

import probe_query as pq

k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p]

CMD = bytes(bytearray([0x13, 0x4A, 0x01] + [0x00] * 16)) + bytes([0x5E])
ROUNDS = 5
LISTEN_S = 6.0

cols = pq.enumerate_collections((0x05AC, 0x024F))
ff02 = None
for (path, upage, usage, inlen, featlen) in cols:
    if upage == 0xFF02:
        ff02 = (path, inlen)
if not ff02:
    print('0xFF02 口未找到')
    sys.exit(1)

path, inlen = ff02
h = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, 0)  # overlapped
if not h or h == -1:
    print(f'0xFF02 打不开 err={ctypes.get_last_error()}')
    sys.exit(1)
print(f'0xFF02 opened inlen={inlen}')
r = pq.Reader(h, report_len=inlen)

for rnd in range(ROUNDS):
    res = r.write(CMD)
    print(f'R{rnd} write -> {res}')
    t0 = time.time()
    got = []
    while time.time() - t0 < LISTEN_S:
        d = r.read(80)
        if d:
            got.append(d)
            print(f'  R{rnd} +{(time.time()-t0)*1000:.0f}ms <- {d.hex(" ")}')
            # 命令应答帧形：[0]=0x13 [1]=0x4a → 解析 commonParser
            if d[0] == 0x13 and d[1] == 0x4A:
                total, cur = d[2], d[3]
                body = d[4:19]
                print(f'    ^^ 命令应答! total={total} cur={cur} body={body.hex(" ")}')
    if not got:
        print(f'  R{rnd}: 零帧')
    time.sleep(0.5)

k32.CloseHandle(h)
print('DONE')
