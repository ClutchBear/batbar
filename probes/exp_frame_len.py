"""验证帧长假说：0xFF02 的 OutputReportByteLength 是 20 还是 21？
Hub 线上帧 = reportID(0x13) + 20B payload（含双 13）= 21B。
"""

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

import probe_query as pq

k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
]

cols = pq.enumerate_collections((0x05AC, 0x024F))
ff02 = next((p, n) for (p, u, s, n, f) in cols if u == 0xFF02)
path, inlen = ff02

# HidP_GetCaps 查 OutputReportByteLength
hid = ctypes.windll.hid
setupapi = ctypes.windll.setupapi


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.USHORT),
        ("Data3", wintypes.USHORT),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _IFD(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_ulonglong),
    ]


class _DETAIL(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 1024)]


class _ATTRS(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_ulong),
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
    ]


class _CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ulong * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
    ]


h = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0, 0)
ppd = ctypes.c_void_p()
hid.HidD_GetPreparsedData(h, ctypes.byref(ppd))
caps = _CAPS()
hid.HidP_GetCaps(ppd, ctypes.byref(caps))
print(
    f"Input={caps.InputReportByteLength} Output={caps.OutputReportByteLength} "
    f"Feature={caps.FeatureReportByteLength}"
)
hid.HidD_FreePreparsedData(ppd)
k32.CloseHandle(h)

OUT_LEN = caps.OutputReportByteLength
r = None
h2 = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, 0)
r = pq.Reader(h2, report_len=caps.InputReportByteLength)

# Hub 姿势 21B：reportID 0x13 + payload [13 4a 01 00×16 5e]（20B）
payload20 = bytes(bytearray([0x13, 0x4A, 0x01] + [0x00] * 16)) + bytes([0x5E])
frame21 = bytes([0x13]) + payload20  # 21B

# 旧姿势 20B：reportID 0x13 + payload [4a 01 00×15 5e]（19B）
payload19 = bytes(bytearray([0x4A, 0x01] + [0x00] * 15)) + bytes([0x5E])
frame20 = bytes([0x13]) + payload19

for tag, frame in [("21B(双13)", frame21), ("20B(旧)", frame20)]:
    if len(frame) != OUT_LEN:
        print(f"{tag}: 长度 {len(frame)} != Output {OUT_LEN}，跳过")
        continue
    buf = ctypes.create_string_buffer(frame, len(frame))
    nw = ctypes.c_uint32(0)
    ctypes.set_last_error(0)
    ok = k32.WriteFile(h2, buf, len(frame), ctypes.byref(nw), None)
    print(f"{tag}: WriteFile ok={ok} nw={nw.value} err={ctypes.get_last_error()}")
    t0 = time.time()
    got = 0
    while time.time() - t0 < 6.0:
        d = r.read(60)
        if d:
            got += 1
            print(f"  +{(time.time() - t0) * 1000:.0f}ms <- {d.hex(' ')}")
    if not got:
        print("  （6s 零帧）")

k32.CloseHandle(h2)
print("DONE")
