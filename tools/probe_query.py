#!/usr/bin/env python3
"""probe_query —— 2.4G 接收器 0xFF02 通道**主动查询**实验探针。

目的：验证 LEOBOG Hi98C Pro 的接收器（3554:FA09）能否被「问」出电量。
我们的被动监听已确认：键盘只在**活动时**推 type=05 电量帧，休眠/关机时
点「立即刷新」什么也问不出 —— 对标项目给出了几种同源（迈从/LEOBOG/Compx）
的主动查询命令，本工具把它们逐个写进 0xFF02 通道，观察接收器回什么。

前提（和 keybeat 一样重要）：**必须先退出 batbar 托盘程序**。HID 每个
collection 只有一条输入队列，两个句柄会互相抢帧；而且写句柄与托盘监听
并存时，回帧可能被托盘吃掉。

▶ 用法（前台跑，你要看输出）
    python tools/probe_query.py                 # 扫描内置候选帧
    python tools/probe_query.py --listen 10     # 只被动听 10 秒（先验证单读者）
    python tools/probe_query.py --cmd "13 4A 01 00 5E"   # 手动追加自定义帧
    python tools/probe_query.py --repeat 3      # 整套候选帧连打 3 遍
    python tools/probe_query.py --set-out       # 换 HidD_SetOutputReport 投递
    python tools/probe_query.py --post-listen 8 # 扫完再听 8 秒，抓唤醒帧

判读：
  · 任何「13 0a 01 00 04 05 xx」= 电量帧（xx%）⇒ **主动查询成功**，可升级为
    双通道（被动监听 + 刷新时主动问）；
  · 其他回帧全部原样 hexdump 落盘 —— 即使解析不出，也是后续逆向的素材；
  · 全无回帧 ⇒ 该接收器只认推送，保持现状。
    【2026-10-12 已实测到此结论】WriteFile + SetOutputReport 两通道 × 7 候选
    全零回帧；post-listen 段抓到过 `11 11 ff fa fd af da da da fa×6`（len=14，
    parse_frame 拒绝，系漏进来的标准 HID 键盘/唤醒报文）——通道活着但无应答。
    ⇒ 3554:FA09 纯推送式，无查询-应答协议，本工具转为取证存档用途。
  · ⚠️ 已知该 collection 输出 reportID = 0x13（2026-10-12 实测：SetOutputReport
    对首字节非 0x13 的帧报 err=1453，0x13 才 OK）。所以 `--cmd` 自定义帧
    首字节必须留 0x13。

⚠️ 写命令的风险：候选帧来自同源项目的公开逆向，对自家固件是常规查询；
   但毕竟是往设备写未知字节。单次实验、随时 Ctrl+C，不要挂机循环。
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import POINTER, WinDLL, byref, wintypes
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from batbar.dongle import TARGETS, FRAME_LEN, parse_frame  # noqa: E402

k32 = WinDLL("kernel32", use_last_error=True)
hid = WinDLL("hid")
setupapi = WinDLL("setupapi")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class IFD(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_ulonglong)]


class DETAIL(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 1024)]


class CAPS(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                ("InputReportByteLength", ctypes.c_ushort),
                ("OutputReportByteLength", ctypes.c_ushort),
                ("FeatureReportByteLength", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort * 17),
                ("NumberLinkCollectionNodes", ctypes.c_ushort),
                ("NumberInputButtonCaps", ctypes.c_ushort),
                ("NumberInputValueCaps", ctypes.c_ushort),
                ("NumberInputDataIndices", ctypes.c_ushort),
                ("NumberOutputButtonCaps", ctypes.c_ushort),
                ("NumberOutputValueCaps", ctypes.c_ushort),
                ("NumberOutputDataIndices", ctypes.c_ushort),
                ("NumberFeatureButtonCaps", ctypes.c_ushort),
                ("NumberFeatureValueCaps", ctypes.c_ushort),
                ("NumberFeatureDataIndices", ctypes.c_ushort)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


INVALID = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x01 | 0x02
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000

k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            wintypes.HANDLE]
k32.ReadFile.restype = wintypes.BOOL
k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         POINTER(wintypes.DWORD), POINTER(OVERLAPPED)]
k32.WriteFile.restype = wintypes.BOOL
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          POINTER(wintypes.DWORD), POINTER(OVERLAPPED)]
k32.GetOverlappedResult.restype = wintypes.BOOL
k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, POINTER(OVERLAPPED),
                                    POINTER(wintypes.DWORD), wintypes.BOOL]
k32.CancelIoEx.restype = wintypes.BOOL
k32.CancelIoEx.argtypes = [wintypes.HANDLE, POINTER(OVERLAPPED)]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
hid.HidD_GetHidGuid.argtypes = [POINTER(GUID)]
hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, POINTER(ctypes.c_void_p)]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, POINTER(CAPS)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidD_SetOutputReport.restype = wintypes.BOOLEAN
hid.HidD_SetOutputReport.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
hid.HidD_GetFeature.restype = wintypes.BOOLEAN
hid.HidD_GetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
hid.HidD_SetFeature.restype = wintypes.BOOLEAN
hid.HidD_SetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [POINTER(GUID), wintypes.LPCWSTR,
                                          wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, POINTER(GUID), wintypes.DWORD,
    POINTER(IFD)]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE, POINTER(IFD), POINTER(DETAIL), wintypes.DWORD,
    POINTER(wintypes.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]


def find_target_path(want: tuple[int, int] | None = None):
    """枚举已知接收器（dongle.TARGETS）的 0xFF02 collection 路径。

    Args:
        want: 指定 (vid, pid)；None = 第一个命中的目标（多接收器时的默认）。
    Returns:
        (path, OutputReportByteLength, 友好名, InputReportByteLength,
         FeatureReportByteLength, NumberFeatureDataIndices)
        或 6 个 None。
    """
    g = GUID()
    hid.HidD_GetHidGuid(byref(g))
    hdev = setupapi.SetupDiGetClassDevsW(byref(g), None, None, 0x02 | 0x10)
    if not hdev or hdev == INVALID:
        return None, None, None, None, None, None
    try:
        i = 0
        while True:
            ifd = IFD()
            ifd.cbSize = ctypes.sizeof(IFD)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, byref(g),
                                                        i, byref(ifd)):
                break
            i += 1
            det = DETAIL()
            det.cbSize = 8
            size = wintypes.DWORD(ctypes.sizeof(DETAIL))
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, byref(ifd), byref(det), size, byref(size), None):
                continue
            up = det.DevicePath.upper()
            hit = None
            for key, name in TARGETS.items():
                vid, pid = key
                if f"VID_{vid:04X}" in up and f"PID_{pid:04X}" in up:
                    if want is None or key == want:
                        hit = (name, key)
                        break
            if not hit:
                continue
            name, _key = hit
            h = k32.CreateFileW(det.DevicePath, 0, FILE_SHARE_RW, None,
                                OPEN_EXISTING, 0, 0)
            if not h or h == INVALID:
                continue
            try:
                pp = ctypes.c_void_p()
                if not hid.HidD_GetPreparsedData(h, byref(pp)):
                    continue
                caps = CAPS()
                hid.HidP_GetCaps(pp, byref(caps))
                hid.HidD_FreePreparsedData(pp)
                if caps.UsagePage == 0xFF02 and caps.InputReportByteLength >= FRAME_LEN:
                    return (det.DevicePath, caps.OutputReportByteLength, name,
                            caps.InputReportByteLength,
                            caps.FeatureReportByteLength,
                            caps.NumberFeatureDataIndices)
            finally:
                k32.CloseHandle(h)
        return None, None, None, None, None, None
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)


def open_rw(path: str):
    """读写双权限 + **重叠 IO** 打开（0xFF02 通道可写，本机 2026-10-12 已验证）。

    ⚠️ FILE_FLAG_OVERLAPPED 必须给：Reader 用带事件的 OVERLAPPED 调 ReadFile，
    若句柄是同步语义，ReadFile 会**在内核里阻塞到有帧为止** —— drain() 的
    20ms「超时」形同虚设，探针卡死在第一条排干读上（09:14 实跑复现）。
    注意 GENERIC_WRITE 与 FILE_FLAG_OVERLAPPED 数值同为 0x40000000 但分属
    不同参数（dwDesiredAccess / dwFlagsAndAttributes），互不冲突。
    """
    return k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW,
                           None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, 0)


class Reader:
    """带超时的重叠读封装 —— 单句柄既是写口也是唯一读口。

    ⚠️ 关键细节：`GetOverlappedResult(bWait=True)` 会**无限等**，不带超时语义。
    必须先 WaitForSingleObject(事件, 超时) 再用 bWait=False 取结果，否则「设备
    不回话」时探针会挂死。
    """

    WAIT_TIMEOUT = 0x0102
    # ⚠️⚠️ 2026-10-12 15:55 破案级修正：ERROR_IO_PENDING 真值是 **997**(0x3E5)。
    # 旧值 99 是笔误 —— 后果：队列暂时无帧时 ReadFile 返回 FALSE+997，与 99
    # 比较失配 ⇒ read() 走「非 PENDING 错误」分支立即 return None，**从不等待
    # 迟到的帧**。命令应答帧都在写入后几十 ms 才到 ⇒ 历轮「零回帧」全部是
    # 探针自身缺陷，不是设备不答！（write 侧同 bug：997 被误当写入长度回显
    # 「OK 997B」。）
    ERROR_IO_PENDING = 997
    ERROR_OPERATION_ABORTED = 1223
    ERROR_SEM_TIMEOUT = 121

    def __init__(self, handle, report_len: int = 0):
        self.h = handle
        self.buf = (ctypes.c_ubyte * 512)()
        self.n = wintypes.DWORD(0)
        # HID 每次 ReadFile 恰好返回一个完整 input report —— 用 caps 里的
        # InputReportByteLength 截断，比信 GetOverlappedResult 的字节数出参
        # 可靠（事件模式下该出参是垃圾，见 read/write 注释）。0 = 未知，退回 n。
        self.report_len = report_len
        self.ev = k32.CreateEventW(None, False, False, None)   # 自动重置事件
        self.last_err = 0
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD

    def _cancel_wait(self, ov):
        k32.CancelIoEx(self.h, byref(ov))
        k32.WaitForSingleObject(self.ev, 1000)

    def _take(self) -> bytes | None:
        """取本次 IO 完成的报文：HID 语义 = 恰好一个 input report。

        ⚠️ 事件模式（hEvent 非 NULL）下 GetOverlappedResult 的字节数出参
        **不被保证填充**（MS 文档：应用应假设事件已置位、直接用完整缓冲）。
        实测 LEOBOG/迈从通道上它是垃圾残值（997/87 等）—— 所以优先按
        report_len 截断，绝不信出参。
        """
        if self.report_len:
            return bytes(self.buf[:self.report_len])
        return bytes(self.buf[:self.n.value]) if self.n.value else None

    def read(self, timeout_ms: int) -> bytes | None:
        """最多等 timeout_ms 毫秒；没帧返回 None（不抛、不挂）。"""
        ov = OVERLAPPED()
        ov.hEvent = self.ev
        ctypes.set_last_error(0)
        ok = k32.ReadFile(self.h, self.buf, 512, byref(self.n), byref(ov))
        if ok:                                # 队列里已有帧，同步直接完成
            return self._take()
        err = ctypes.get_last_error()
        if err != self.ERROR_IO_PENDING:
            self.last_err = err
            return None
        if k32.WaitForSingleObject(self.ev, timeout_ms) == self.WAIT_TIMEOUT:
            self._cancel_wait(ov)             # 超时：取消这次 IO，否则句柄状态脏
            return None
        ctypes.set_last_error(0)
        got = k32.GetOverlappedResult(self.h, byref(ov), byref(self.n), False)
        if not got:
            self.last_err = ctypes.get_last_error()
            return None
        return self._take()

    def write(self, payload: bytes, timeout_ms: int = 1500) -> int | None:
        """写一帧报告；成功返回写入字节数，失败返回错误码（None 语义由调用方判）。

        ⚠️ 成功时直接回 len(payload)：事件模式下 GetOverlappedResult 的字节数
        出参不被保证填充（同 read 的 _take 注释），信它就会出现「OK 997B」这种
        垃圾回显。HID 写是整 report 投递，完成即全量，长度由 payload 自明。
        """
        buf = ctypes.create_string_buffer(payload, len(payload))
        nw = wintypes.DWORD(0)
        ov = OVERLAPPED()
        ov.hEvent = self.ev
        ctypes.set_last_error(0)
        ok = k32.WriteFile(self.h, buf, len(payload), byref(nw), byref(ov))
        if ok:
            return len(payload)
        err = ctypes.get_last_error()
        if err != self.ERROR_IO_PENDING:
            return err
        if k32.WaitForSingleObject(self.ev, timeout_ms) == self.WAIT_TIMEOUT:
            self._cancel_wait(ov)
            return -1                          # -1 = 超时未确认
        ctypes.set_last_error(0)
        if k32.GetOverlappedResult(self.h, byref(ov), byref(nw), False):
            return len(payload)
        return ctypes.get_last_error()


def drain(rd: Reader, limit: int = 32) -> list[bytes]:
    """排干输入队列里的旧帧（抄 device-battery-tray 的 DrainReports），
    保证「写命令 → 读回帧」的对应关系不被历史残留污染。"""
    out = []
    for _ in range(limit):
        d = rd.read(20)
        if d is None:
            break
        out.append(d)
    return out


def build_candidates(out_len: int) -> list[tuple[str, bytes]]:
    """候选查询帧全集（来源逐条标注，勿凭记忆加帧）。

    返回的每条已经是**可直接 WriteFile 的报告缓冲区**：首字节 = reportID，
    其后数据补齐到 out_len（OutputReportByteLength）。
    """
    def pack(payload: bytes) -> bytes:
        """payload 含 reportID 首字节，补齐/裁剪到 out_len。"""
        return payload.ljust(out_len, b"\x00")[:out_len]

    def cs_xor(frame: bytes) -> bytes:
        """最后一字节 = 前面所有字节异或（MCHOSE-Battery 鼠标命令的校验法）。"""
        b = bytearray(frame)
        acc = 0
        for x in b[:-1]:
            acc ^= x
        b[-1] = acc & 0xFF
        return bytes(b)

    cands: list[tuple[str, bytes]] = []
    # 1) device-battery-tray（迈从 K99 V2 / 41E4:2001 Compx 方案）：
    #    reportID 0x13、命令字节 0x4A、校验 0x5E，原版 64 字节。
    #    我们通道 OutputReportByteLength 多半是 20，先打截断版；无效再用 --cmd 打 64B 原版。
    cands.append(("dbt-K99v2 0x13:4A 截断", pack(bytes.fromhex("134a01005e"))))
    cands.append(("dbt-K99v2 0x13:4A 零填充", pack(bytes.fromhex("134a0100") + b"\x00" * 15 + b"\x5e")))
    # 2) MCHOSE-Battery 鼠标（3837:1014，UP=0xFF01）：4D 01 01 00 00 09 00 00 <异或>
    mouse = cs_xor(bytes.fromhex("4d01010000090000"))
    cands.append(("mcb-mouse 0x4D:01", pack(mouse)))
    # 3) MCHOSE-Battery 键盘 K99V3 命令通道（UP=0x0001，不在本 collection！）
    #    Z 帧 55 cmd 00 cs size offLo offHi …；cmd 含义未证实，仅试 0x00/0x01。
    #    ⚠️ 这批要打到 usagePage=0x0001 的接口才有意义 —— 打到 0xFF02 大概率无回应，
    #    作为「同族镜像」的对照组保留。
    for cmd in (0x00, 0x01):
        z = bytearray([0x55, cmd, 0x00, 0x00, 0x08, 0x00, 0x00, 0x00])
        acc = 0
        for x in z[:2] + z[4:]:
            acc ^= x
        z[3] = acc & 0xFF
        cands.append((f"mcb-kbd 0x55 cmd{cmd:02x}（校验猜）", pack(b"\x00" + bytes(z))))
    # 4) 自研同族镜像：把推送帧的 type=05 反过来当查询发（问「报一下电量」）。
    #    reportID 可能要求 0x00 或 0x13，两种都试；sum8 校验照抄推送帧算法。
    for rid in (0x00, 0x13):
        f = bytearray([rid, 0x13, 0x0a, 0x01, 0x00, 0x04, 0x05, 0x00])
        f += b"\x00" * (out_len - len(f) - 1)
        f.append(sum(f[1:9]) & 0xFF)
        cands.append((f"自研镜像 0x{rid:02x}:…04 05（sum8）", pack(bytes(f))))
    return cands


def enumerate_collections(want: tuple[int, int]):
    """枚举目标 VID:PID 的**全部** HID collection（不限 0xFF02）。

    背景：0xFF02 主监听口已证「不推不答无 feature」，但该设备还有
    COL02-COL06 多个接口（其中 COL06 也是供应商定义）——电量帧可能从
    别的 collection 推，feature report 也可能藏在别的接口。本函数把
    每个接口的 caps 原样带回来，供 --collections 模式逐一监听+拉取。
    """
    g = GUID()
    hid.HidD_GetHidGuid(byref(g))
    hdev = setupapi.SetupDiGetClassDevsW(byref(g), None, None, 0x02 | 0x10)
    out = []
    if not hdev or hdev == INVALID:
        return out
    vidtag = f"VID_{want[0]:04X}"
    pidtag = f"PID_{want[1]:04X}"
    try:
        i = 0
        while True:
            ifd = IFD()
            ifd.cbSize = ctypes.sizeof(IFD)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, byref(g),
                                                        i, byref(ifd)):
                break
            i += 1
            det = DETAIL()
            det.cbSize = 8
            size = wintypes.DWORD(ctypes.sizeof(DETAIL))
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, byref(ifd), byref(det), size, byref(size), None):
                continue
            up = det.DevicePath.upper()
            if vidtag not in up or pidtag not in up:
                continue
            h = k32.CreateFileW(det.DevicePath, 0, FILE_SHARE_RW, None,
                                OPEN_EXISTING, 0, 0)
            if not h or h == INVALID:
                continue
            try:
                pp = ctypes.c_void_p()
                if not hid.HidD_GetPreparsedData(h, byref(pp)):
                    continue
                caps = CAPS()
                hid.HidP_GetCaps(pp, byref(caps))
                hid.HidD_FreePreparsedData(pp)
                out.append((det.DevicePath, caps.UsagePage, caps.Usage,
                            caps.InputReportByteLength,
                            caps.FeatureReportByteLength))
            finally:
                k32.CloseHandle(h)
        return out
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)


def probe_collections(want, seconds: float, w, cmds: list[bytes] | None = None,
                      out_len: int = 20) -> None:
    """--collections 主体：同时只读打开目标的全部 collection，轮询监听
    seconds 秒（期间敲键），随后对声明了 feature 的接口逐个穷举读取。

    cmds 非空时（官方查询帧实验）：先对每个 collection 试 WriteFile 投递
    （0xC0000000 读+写权限句柄才给写；首字节须=输出 reportID 否则 err=1453），
    写成功后继续监听 —— 应答帧可能出现在**任何**接口的 input 队列，
    逐口分别打计数才能定位应答通道。

    轮询而非多线程：每接口 read(60ms) 顺序转一圈，一圈 ~0.4s，抓敲键
    产生的帧足够（帧在内核队列里等着，不会丢）。
    """
    cols = enumerate_collections(want)
    if not cols:
        w(f"[collections] 没枚举到 {want[0]:04X}:{want[1]:04X} 的任何接口")
        return
    w(f"[collections] {len(cols)} 个接口，监听 {seconds:.0f}s（请持续敲键盘）：")
    w("  ⚠️ 含键盘/鼠标标准口 —— 监听期间该键盘的按键可能被探针读走导致"
      "偶尔丢键，属预期，扫完即恢复。")
    opened = []
    try:
        for (p, upage, usage, inlen, featlen) in cols:
            tail = p.split("#")[-1][:12]
            label = f"up=0x{upage:04X}/us=0x{usage:04X} in={inlen} ft={featlen} {tail}"
            # 读写双权限：--collections 若带官方查询帧需要 WriteFile 投递。
            # ⚠️ 但标准键盘/鼠标 collection 被 kbdhid/mouhid 按「只读」放行：
            # GENERIC_WRITE 直接 err=5（2026-10-12 16:03 实测：同步只开
            # GENERIC_READ 就能 OPEN）—— 电量应答帧很可能就从这些口的
            # input 推出来，必须带只读回退，否则永远漏听。
            h = k32.CreateFileW(p, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW,
                                None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, 0)
            if not h or h == INVALID:
                h = k32.CreateFileW(p, GENERIC_READ, FILE_SHARE_RW,
                                    None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, 0)
            if not h or h == INVALID:
                w(f"  [x] 打不开 {label} err={ctypes.get_last_error()}")
                continue
            opened.append((label, Reader(h, report_len=inlen or 0), h, featlen, p))
            w(f"  [ok] {label}")
        counts = {lab: 0 for lab, _r, _h, _f, _p in opened}

        def pull_features(tag: str) -> dict[str, dict[int, bytes]]:
            """对所有 featlen>0 的口拉一遍 feature，返回 {label: {rid: bytes}}。

            ⚠️ 必须用 path 重开**同步**句柄拉：HidD_GetFeature 对重叠句柄
            行为未定义（见 probe_feature 注释）。
            """
            res: dict[str, dict[int, bytes]] = {}
            for lab, _rd, _h, featlen, p in opened:
                if featlen:
                    hits = feature_hits(p, featlen)
                    res[lab] = hits
                    for rid, data in sorted(hits.items()):
                        pf = parse_frame(data)
                        w(f"  [{tag}][feature {lab}] rid=0x{rid:02x} "
                          f"{data.hex(' ')}" + (f"  {pf}" if pf else ""))
            return res

        # 基线：写命令前先拉一遍 feature —— 若官方查询的「应答」表现为
        # feature 值被刷新（而非 input 回帧），只有前后对比才能看出来。
        base = pull_features("PRE") if cmds else {}
        # 官方查询帧投递（来自 MCHOSE Hub bundle 的 wirelessCommand，
        # `13 4a 01 00 …` = 迈从家族原生 getBattery 命令）。逐口 WriteFile：
        # 成功的口记下，应答可能从**任何**口的 input 队列回来。
        for cmd in cmds or []:
            payload = cmd.ljust(out_len, b"\x00")[:out_len]
            for lab, rd, _h, _f, _p in opened:
                res = rd.write(payload)
                if res and res > 0:
                    w(f"  WRITE -> [{lab}] {payload.hex(' ')} OK {res}B")
                else:
                    w(f"  WRITE -> [{lab}] FAIL code={res}")
        end = time.time() + seconds
        while time.time() < end:
            for lab, rd, _h, _f, _p in opened:
                d = rd.read(60)
                if d:
                    counts[lab] += 1
                    pf = parse_frame(d)
                    # 键盘口（us=0x06）敲键必刷屏，只打前 3 帧；其余口全打
                    if counts[lab] <= 3 or "0xFF02" in lab:
                        w(f"  <- [{lab}] {d.hex(' ')}"
                          + (f"   {pf}" if pf else ""))
        w("  帧计数：" + "; ".join(f"{lab}={n}" for lab, n in counts.items()))
        # feature 穷举（收尾）：与基线对比，值变了 = 官方查询刷新了 feature
        if cmds:
            post = pull_features("POST")
            for lab, hits in post.items():
                for rid, data in hits.items():
                    old = base.get(lab, {}).get(rid)
                    if old is not None and old != data:
                        w(f"  ★★ FEATURE 被刷新 [{lab}] rid=0x{rid:02x}: "
                          f"{old.hex(' ')} -> {data.hex(' ')}")
                        w("     ⇒ 官方查询命令有效！应答机制=刷新 feature，"
                          "托盘可在刷新前先发命令再拉取")
            # gx87-studio 姿势：input 投递之外，再试 feature 通道内的
            # SetFeature 查询 → GetFeature 拉回（同协议族真机实锤的应答机制）
            w("  [roundtrip] gx87-studio 姿势：SetFeature 查询帧 → GetFeature：")
            for lab, _rd, _h, featlen, p in opened:
                feature_roundtrip(p, featlen, w, lab)
        else:
            for lab, _rd, _h, featlen, p in opened:
                if featlen:
                    w(f"  [feature] {lab}")
                    probe_feature_by_path(p, featlen, w)
    finally:
        for _item in opened:
            _lab, _rd, h = _item[0], _item[1], _item[2]
            try:
                k32.CancelIoEx(h, None)
            except Exception:  # noqa: BLE001
                pass
            k32.CloseHandle(h)


def feature_roundtrip(path: str, featlen: int, w, label: str) -> None:
    """GX87-studio 姿势的主动查询：SetFeature(查询帧) → 短 sleep → GetFeature。

    zerom-code/gx87-studio（迈从另一把键盘 GX87 的配置器，SinoWealth 同协议
    族）逆向 + 真机实锤：官方 getBattery 的应答**不走 input 队列**，而是固件
    把结果写进 feature report，主机 HidD_SetFeature 触发查询、随即 HidD_GetFeature
    拉回（VendorHidDevice.Request = Send→sleep 2ms→Receive）。这正好解释我们
    K87「input 零帧 + WriteFile 不答」但 feature 口有值的现象。

    对每个 feature 口用两条候选查询帧（补齐/截断到 featlen，首字节=目标
    feature reportID 0x06）做前后对比：
      06 87 00 00 01 00 02 00  = gx87-studio BatteryRequest（opcode 0x87）
      06 4A 01 00 …            = Hub bundle 无线 getBattery 的 4A 挪进 feature 通道
    """
    if not featlen or featlen < 2:
        return
    h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW,
                        None, OPEN_EXISTING, 0, 0)  # 同步句柄，与 feature_hits 同姿势
    if not h or h == INVALID:
        w(f"  [rt] {label} 句柄打不开 err={ctypes.get_last_error()}")
        return
    try:
        # (预拉取rid, 查询帧payload, 说明)。rid 双候选实锤依据（Hub bundle）：
        # 迈从家族默认 wireRid=6（eJe=6，K87 不在 CE 例外表）→ 0x06；
        # G98_V2 等例外表型号 wirelessRId=19（oF=19）→ 0x13 一并试绝死角。
        # payload 首字节是协议内 opcode（与 HID rid 是两回事）：
        # 0x87 = gx87-studio BatteryRequest；0x4A = bundle 无线 getBattery opcode。
        cands = [
            (0x06, bytes([0x87, 0x00, 0x00, 0x01, 0x00, 0x02, 0x00]), "87/gx87"),
            (0x06, bytes([0x4A, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]), "4A/无线"),
            (0x13, bytes([0x4A, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]), "4A@13"),
        ]
        for rid, core, note in cands:
            q = (bytes([rid]) + core + b"\x00" * featlen)[:featlen]
            buf = ctypes.create_string_buffer(featlen)
            buf[0] = bytes([rid])
            ctypes.set_last_error(0)
            if not hid.HidD_GetFeature(h, buf, featlen):
                w(f"  [rt] {label} [{note}] 预拉取 rid=0x{rid:02x} 失败 "
                  f"err={ctypes.get_last_error()}")
                continue
            before = bytes(buf.raw[:featlen])
            ctypes.set_last_error(0)
            set_buf = ctypes.create_string_buffer(q, featlen)
            ok_set = hid.HidD_SetFeature(h, set_buf, featlen)
            serr = ctypes.get_last_error()
            time.sleep(0.05)  # gx87 用 2ms；本机放宽到 50ms 防固件没备好答案
            buf2 = ctypes.create_string_buffer(featlen)
            buf2[0] = bytes([rid])
            ctypes.set_last_error(0)
            ok_get = hid.HidD_GetFeature(h, buf2, featlen)
            after = bytes(buf2.raw[:featlen]) if ok_get else b""
            w(f"  [rt] {label} [{note}] 查询帧 {q.hex(' ')} "
              f"set={'OK' if ok_set else f'FAIL({serr})'} "
              f"get={'OK' if ok_get else 'FAIL'}")
            if not ok_get:
                continue
            # 关键判据（2026-10-12 15:38 实测修正）：设备可能只把**我们写进去的
            # opcode 字节回显**（byte[1]: 00→87/4a），其余保持原值 —— 那不是刷新。
            # 真·刷新 = 存在某字节 after[i] 既不等于旧值 before[i]、也不等于我们
            # 写的 q[i]（即设备自己产出的新值，典型是电量应答落进 payload）。
            dev_changed = [i for i in range(min(len(after), len(before), len(q)))
                           if after[i] != before[i] and after[i] != q[i]]
            echo_only = [i for i in range(min(len(after), len(before), len(q)))
                         if after[i] != before[i] and after[i] == q[i]]
            if dev_changed:
                pf = parse_frame(after)
                w(f"  ★★ FEATURE 被查询帧刷新 [{label}] rid=0x{rid:02x}: "
                  f"{before.hex(' ')} -> {after.hex(' ')}"
                  f"（设备改写字节 {[hex(i) for i in dev_changed]}）"
                  + (f"  {pf}" if pf else ""))
                w("     ⇒ gx87-studio 姿势在本机成立！刷新链路 = "
                  "SetFeature 查询 → GetFeature 拉回")
            elif echo_only:
                w(f"     （仅回显：设备只写回了我们投递的 opcode 字节 "
                  f"{[hex(i) for i in echo_only]}，payload 未变 ⇒ 非电量应答，"
                  "本 feature 是主机可写暂存区）")
    finally:
        k32.CloseHandle(h)


def feature_hits(path: str, featlen: int) -> dict[int, bytes]:
    """穷举 feature ID 0x00-0x1F，返回 {rid: bytes}（失败=空 dict）。

    每次现开现关**同步**句柄 —— 与 feature24.read_feature 同姿势（实测
    可靠）；--collections 的监听句柄是重叠语义，不共用。
    """
    res: dict[int, bytes] = {}
    if not featlen:
        return res
    h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW,
                        None, OPEN_EXISTING, 0, 0)
    if not h or h == INVALID:
        return res
    try:
        for rid in range(0x20):
            buf = ctypes.create_string_buffer(featlen)
            buf[0] = bytes([rid])
            ctypes.set_last_error(0)
            if hid.HidD_GetFeature(h, buf, featlen):
                res[rid] = bytes(buf.raw[:featlen])
        return res
    finally:
        k32.CloseHandle(h)


def probe_feature_by_path(path: str, featlen: int, w) -> None:
    """穷举并打印（--collections 无命令分支用）。"""
    hits = feature_hits(path, featlen)
    if not hits:
        w(f"    全部失败（无可读 feature report，featlen={featlen}）")
        return
    for rid, data in sorted(hits.items()):
        pf = parse_frame(data)
        w(f"  ★ ID=0x{rid:02x}  {data.hex(' ')}" + (f"   <- {pf}" if pf else ""))


def probe_feature(hd, featlen: int, w) -> None:
    """暴力扫 Feature Report ID 0x00–0x1F：HidD_GetFeature 是同步 IOCTL，
    不需要固件「应答」——设备里存着什么 report，主机就能直接拉什么。

    很多国产键盘（含部分公版方案）把电量放在 feature report，input/output
    两条通道全零帧时，这是最后一根稻草。对不存在的 ID 该调用立即失败、
    无副作用，所以可以安全穷举。
    """
    if not featlen:
        w("[feature] FeatureReportByteLength=0 且 NumberFeatureDataIndices=0"
          " —— 该 collection 未声明任何 feature report，跳过穷举。")
        return
    w(f"[feature] 穷举 reportID 0x00-0x1F（缓冲区 {featlen}B）：")
    hits = 0
    for rid in range(0x20):
        buf = ctypes.create_string_buffer(featlen)
        buf[0] = bytes([rid])
        ctypes.set_last_error(0)
        ok = hid.HidD_GetFeature(hd, buf, featlen)
        if ok:
            hits += 1
            data = bytes(buf.raw[:featlen])
            pf = parse_frame(data)
            w(f"  ★ ID=0x{rid:02x}  {data.hex(' ')}" + (f"   <- {pf}" if pf else ""))
    if not hits:
        w("  全部失败（该 collection 无可读 feature report）"
          f"，最后 err={ctypes.get_last_error()}")


def send_and_watch(rd: Reader, payload: bytes, wait_s: float,
                   logf, *, set_out: bool = False) -> list[bytes]:
    """写一帧报告（payload 已含 reportID 且补齐长度），随后收集 wait_s 秒内
    的所有回帧。写失败也如实记录。

    set_out=True 走 HidD_SetOutputReport（IOCTL_HID_SET_OUTPUT_REPORT）而非
    WriteFile：部分厂商 collection 的中断 OUT 端点是死的、只认这条 IOCTL。
    两种投递方式都零回帧，才算真把「主动查询」这条路证否。
    """
    if set_out:
        buf = ctypes.create_string_buffer(payload, len(payload))
        ok = hid.HidD_SetOutputReport(rd.h, buf, len(payload))
        res = len(payload) if ok else ctypes.get_last_error()
        status = f"OK {res}B (SetOutputReport)" if ok else f"FAIL err={res}"
    else:
        res = rd.write(payload)
        status = f"OK {res}B" if res and res > 0 else f"FAIL code={res}"
    line = (f"[{time.strftime('%H:%M:%S')}] WRITE {payload.hex(' ')} -> {status}\n")
    print("   " + line.rstrip(), flush=True)
    logf.write(line)
    replies = []
    if set_out:
        if not hid_ok(res):
            return replies
    elif not res or res <= 0:
        return replies
    t_end = time.time() + wait_s
    while time.time() < t_end:
        d = rd.read(300)
        if d is None:
            continue
        replies.append(d)
        pf = parse_frame(d)
        tag = f"PARSED {pf[0]}={pf[1]}" if pf else "RAW(hex)=" + d.hex(" ")
        line = f"[{time.strftime('%H:%M:%S')}]   REPLY +{tag}\n"
        print("   " + line.rstrip(), flush=True)
        logf.write(line)
    return replies


def hid_ok(res) -> bool:
    """HidD_SetOutputReport 成功判据：返回非 0（BOOLEAN TRUE）。res 此时是
    成功时的写入长度或失败时的错误码，>0 即成功。"""
    return bool(res) and res > 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="0xFF02 主动查询探针")
    ap.add_argument("--listen", type=float, default=0, metavar="SEC",
                    help="只做被动监听 N 秒（验证单读者 / 看当前自发帧）")
    ap.add_argument("--cmd", action="append", default=[], metavar="HEX",
                    help="额外手动帧（空格分隔 hex，可多次）")
    ap.add_argument("--repeat", type=int, default=1, help="整套候选打几遍")
    ap.add_argument("--wait", type=float, default=2.5, metavar="SEC",
                    help="每发命令后的回帧观察窗（默认 2.5s）")
    ap.add_argument("--out", default="_tmp", help="日志目录")
    ap.add_argument("--force", action="store_true",
                    help="即使检测到托盘在跑也继续（风险自担）")
    ap.add_argument("--set-out", action="store_true",
                    help="用 HidD_SetOutputReport(IOCTL) 投递而非 WriteFile；"
                         "WriteFile 全零回帧时换这条通道再证一次")
    ap.add_argument("--post-listen", type=float, default=0, metavar="SEC",
                    help="扫完候选后再纯被动听 N 秒 —— 抓「唤醒瞬间推送帧」"
                         "（唤醒常发生在扫描结束后几秒，旧版会整段漏掉）")
    ap.add_argument("--target", metavar="VID:PID|名称",
                    help="指定扫哪个接收器（如 05AC:024F 或 迈从键盘）；"
                         "默认第一个枚举到的。多接收器时用于分别实验")
    ap.add_argument("--feature", action="store_true",
                    help="穷举 HidD_GetFeature（reportID 0x00-0x1F）——"
                         "input/output 两通道都零帧时试最后一条标准 HID 通道")
    ap.add_argument("--collections", type=float, default=0, metavar="SEC",
                    help="全接口扫描：同时监听目标接收器的**所有** collection "
                         "SEC 秒 + 对声明 feature 的接口穷举（0xFF02 口已证伪后"
                         "的最后系统性排查）；需配 --target")
    ap.add_argument("--official", action="store_true",
                    help="配合 --collections/--feature：投递 Hub 官方 getBattery"
                         " 无线帧 13 4a 01 00… 并做 PRE/POST 对比；同时按 "
                         "gx87-studio 姿势试 SetFeature(06 87/4A…)→GetFeature "
                         "查询帧往返（同协议族真机实锤的应答机制）")
    args = ap.parse_args()

    want = None
    # 别名：友好名改成「类别 品牌+型号」后，老命令行里的「迈从键盘」等
    # 短名不能失效（历史日志/提醒/肌肉记忆都引用着旧写法）。
    _ALIASES = {
        (0x05AC, 0x024F): ("迈从", "k87", "mchose"),
        (0x3554, 0xFA09): ("leobog", "hi98"),
    }
    if args.target:
        raw = args.target.strip()
        low = raw.lower()
        for key, name in TARGETS.items():
            aliases = _ALIASES.get(key, ())
            if (raw in (name, f"{key[0]:04X}:{key[1]:04X}",
                        f"{key[0]:04x}:{key[1]:04x}")
                    or any(a in low for a in aliases)):
                want = key
                break
        if want is None:
            try:
                v, p = raw.replace("-", ":").split(":")
                want = (int(v, 16), int(p, 16))
            except ValueError:
                w(f"[x] --target 无法解析：{raw}"
                  "（用 05AC:024F / 3554:FA09 或 迈从/leobog）")
                return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logp = out / f"probe-query-{datetime.now():%Y%m%d-%H%M%S}.md"
    logf = logp.open("w", encoding="utf-8")

    def w(s: str = "") -> None:
        print(s, flush=True)
        logf.write(s + "\n")
        logf.flush()

    w(f"# probe_query {datetime.now():%Y-%m-%d %H:%M:%S}")
    # 自检对所有模式生效（含 --listen）：托盘在跑会按单读者原则偷帧，
    # 被动监听拿到「零帧」可能是被偷而不是设备静默 —— 这个混淆必须排除。
    if not args.force:
        try:
            from batbar import singleinstance
            if singleinstance.already_running():
                w("\n[!] 检测到 batbar 托盘实例正在运行 —— 它会与本探针抢帧。")
                w("    请先右键托盘「退出」（或任务管理器结束），再跑本工具。")
                w("    （确认要强行试：加 --force，但数据大概率收不全）")
                return 2
        except Exception:  # noqa: BLE001
            pass

    # --collections：全接口扫描，不依赖 0xFF02 口，必须先分流；需明确 --target
    if args.collections > 0:
        if want is None:
            w("[x] --collections 必须配 --target（多接收器时无法确定扫哪个）")
            logf.close()
            return 1
        try:
            # 官方无线 getBattery（bundle 实锤）：wirelessCommand = `13 4a 01`
            # + 补零到 19 字节，末尾再追加**校验和** `X3e(cmd)` = 所有字节按
            # 十六进制求和（0x13+0x4a+0x01 = 0x5e）。⚠️ 2026-10-12 15:39 破案：
            # 此前发无校验的 `13 4a 01 00…` 一直零回帧，根因是固件校验失败丢弃
            # —— device-battery-tray 的 `…5E` 尾巴是**对的**，不是它自加的。
            # 凑满 0xFF02 的 OutputReportByteLength=20，与官方逐字节对齐：
            # [0x13(reportID), 0x4a, 0x01] + 16×0x00 + [校验和]  = 20 字节。
            body = bytearray([0x13, 0x4A, 0x01] + [0x00] * 16)  # 19 字节
            chk = sum(body) & 0xFF                              # 0x13+0x4a+0x01=0x5e
            cmd = bytes(body) + bytes([chk])                    # 20 字节
            cmds = [cmd] if args.official else None
            if args.official:
                w(f"[official] getBattery 帧（含校验和 0x{chk:02x}）: "
                  f"{cmds[0].hex(' ')}")
            probe_collections(want, args.collections, w, cmds=cmds)
        finally:
            w(f"\n日志：{logp.resolve()}")
            logf.close()
        return 0

    path, outlen, tname, inlen, featlen, featn = find_target_path(want)
    if not path:
        if want is not None:
            w(f"[x] 没找到 {want[0]:04X}:{want[1]:04X} 的 0xFF02 collection。"
              f"确认该接收器插着、键盘处于 2.4G 模式。")
        else:
            w("[x] 没找到任何已知接收器的 0xFF02 collection。"
              "确认接收器插着、键盘 2.4G 模式。")
        return 1
    w(f"[ok] 目标接口: {tname} {path[-60:]}（OutputReportByteLength={outlen}"
      f"，InputReportByteLength={inlen}，FeatureReportByteLength={featlen}"
      f"，NumberFeatureDataIndices={featn}）")
    out_len = outlen if outlen and outlen >= 1 else FRAME_LEN

    hd = open_rw(path)
    if not hd or hd == INVALID:
        w(f"[x] 读写打开失败 err={ctypes.get_last_error()}")
        return 1

    # --feature：主动拉 Feature Report（HidD_GetFeature 是同步 IOCTL，不需要
    # 固件「应答」，主机直接读设备里存好的 report）。很多国产键盘电量藏在
    # feature report，input/output 两条通道都零帧时，这是最后一根稻草。
    if args.feature:
        try:
            hits = feature_hits(path, featlen)
            if not hits:
                w(f"[feature] 穷举 0x00-0x1F 全部失败（featlen={featlen}；"
                  "0 表示该口未声明 feature）")
            for rid, data in sorted(hits.items()):
                pf = parse_frame(data)
                w(f"  ★ ID=0x{rid:02x}  {data.hex(' ')}"
                  + (f"   <- {pf}" if pf else ""))
            if args.official:
                w("[feature+official] gx87-studio 姿势查询帧往返：")
                feature_roundtrip(path, featlen, w, tname)
        finally:
            logf.close()
        return 0

    rd = Reader(hd, report_len=inlen or 0)
    try:
        if args.listen > 0:
            w(f"[listen] 纯被动 {args.listen:.0f}s：")
            end = time.time() + args.listen
            while time.time() < end:
                d = rd.read(300)
                if d:
                    pf = parse_frame(d)
                    w(f"  {d.hex(' ')}" + (f"   <- {pf}" if pf else ""))
            return 0

        cands = build_candidates(out_len)
        for i, hx in enumerate(args.cmd):
            payload = bytes.fromhex(hx.replace(" ", ""))
            if len(payload) != out_len:
                payload = payload.ljust(out_len, b"\x00")[:out_len]
            cands.append((f"manual-{i}", payload))

        old = drain(rd)
        w(f"[drain] 排干旧帧 {len(old)} 条"
          + ("：" + "; ".join(d.hex(" ") for d in old[:4]) if old else ""))

        for rnd in range(args.repeat):
            for name, payload in cands:
                w(f"\n## 候选 {name}  (第 {rnd + 1} 遍{'，SetOutputReport' if args.set_out else ''})")
                replies = send_and_watch(rd, payload, args.wait, logf,
                                         set_out=args.set_out)
                bat = [parse_frame(d) for d in replies if parse_frame(d)]
                bats = [b for b in bat if b and b[0] == "battery"]
                if bats:
                    w(f"  ★★ 电量帧命中: {bats}  ← **主动查询成功**")
                elif bat:
                    w(f"  ✓ 有解析帧但无电量: {bat}")
                elif replies:
                    w(f"  ? 回了 {len(replies)} 帧但都解析不了（素材已全量落盘）")
                else:
                    w("  - 无回帧")
                drain(rd)     # 回帧可能有延迟，收尾再排一次防串台

        if args.post_listen > 0:
            w(f"\n[post-listen] 扫完后再被动听 {args.post_listen:.0f}s"
              "（此刻敲一下键盘 —— 唤醒帧会推上来）：")
            end = time.time() + args.post_listen
            while time.time() < end:
                d = rd.read(300)
                if d:
                    pf = parse_frame(d)
                    w(f"  {d.hex(' ')}" + (f"   <- {pf}" if pf else ""))
    finally:
        try:
            k32.CancelIoEx(hd, None)
        except Exception:  # noqa: BLE001
            pass
        k32.CloseHandle(hd)
        w(f"\n日志：{logp.resolve()}")   # ★ 必须在 logf.close() 之前写
        logf.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[中断] 已写入部分见日志文件")
        sys.exit(130)
