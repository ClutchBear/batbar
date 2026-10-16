"""Windows 侧 HID transport：用 hidapi（cython-hidapi）打开罗技 vendor collection。

只在本模块 import hid —— 这样不需要碰 USB 的代码（如纯逻辑层单测）即使没装 hidapi 也能跑
（纯逻辑层根本不读 USB，读 USB 的是 Windows 侧的 agent）。

罗技接收器（Bolt C53F / Unifying C52x）暴露多个 HID collection，HID++ 走
usage_page=0xFF00/0xFF40/0xFF41/0xFFB0 系的 vendor collection（本机 C53F 是
MI_02 上的 "符合 HID 标准的供应商定义设备"）。选择逻辑对齐 Solaar base.py：
优先 usage_page 0xFF00 且 usage 0xFF01/0xFF02/0xFF09/0x0003（Bolt），
拿不到就退回该 VID/PID 的第一个可枚举设备。
"""

from __future__ import annotations

import logging

import hid  # cython-hidapi：import 名是 hid（pip 包名是 hidapi！）

from . import hidpp as H
from .session import Transport, TransportClosed

log = logging.getLogger("batbar.transport")

# 匹配罗技 HID++ collection 的 usage_page 集合
VENDOR_PAGES = (0xFF00, 0xFF40, 0xFF41, 0xFFB0, 0x0001)  # 0x0001 兜底留给 BT 直连的 Generic


def is_logitech(d: dict) -> bool:
    return d.get("vendor_id") == 0x046D


def find_devices(bt_names: tuple[str, ...] = ()) -> list[dict]:
    """枚举候选 HID++ 设备：罗技 vendor collection + 可选蓝牙直连设备。"""
    out = []
    for d in hid.enumerate():
        lp = d.get("usage_page", d.get("usage_page_raw", 0))
        if is_logitech(d) and lp in VENDOR_PAGES[:4]:
            out.append(d)
        elif bt_names and any(n.lower() in (d.get("product_string") or "").lower() for n in bt_names):
            out.append(d)
    # 罗技接收器的 vendor collection 排在最前
    out.sort(
        key=lambda d: 0 if d.get("vendor_id") == 0x046D and d.get("usage_page") in VENDOR_PAGES[:4] else 1
    )
    return out


def open_path(path: str) -> hid.device:
    dev = hid.device()
    dev.open_path(path.encode() if isinstance(path, str) else path)
    dev.set_nonblocking(True)  # 读循环自己管超时
    return dev


# Windows HID 小驱动要求 WriteFile 长度 == OutputReportByteLength（含 report id 字节），
# 差一字节直接失败。HID++ 帧本身只有 7B(短)/20B(长)，但罗技固件声明的 report 大小
# 常见为 8/20/64 —— 故按候选长度逐个补零试探，命中即缓存。Linux 不校验，Solaar 才敢直发。
WRITE_SIZES = (7, 8, 20, 64)


def pad_to(frame: bytes, n: int) -> bytearray:
    buf = bytearray(n)
    buf[: min(n, len(frame))] = frame[:n]
    return buf


def try_write(dev: hid.device, frame: bytes, sizes: tuple[int, ...] = WRITE_SIZES) -> int | None:
    """按候选长度依次写，返回第一个成功的长度；全失败返回 None。"""
    for n in sizes:
        try:
            r = dev.write(pad_to(frame, n))
        except (OSError, ValueError):
            r = -1
        if r >= 0:
            return n
    return None


class HidapiTransport(Transport):
    """把 hidapi device 适配成 session.Transport（write 接收器寻址帧）。

    ⚠️ Windows HID 按 report ID 分流输入帧：罗技 Bolt（本机 C53F 实测）把
    0x10 短帧声明在 A collection、0x11 长帧的 input report 声明在同页另一个
    B collection —— ReadFile(A) 永远读不到 B 的帧。所以写用可写的 primary，
    读要同时轮询 primary + 同 usage_page 的全部兄弟 collection（readers）。
    """

    def __init__(self, dev: hid.device, frame_len: int | None = None, readers: tuple | list = ()):
        self._dev = dev
        self._closed = False
        self._frame_len = frame_len  # 已探明的写长度，避免每帧重试
        self._readers = [dev] + [r for r in readers if r is not dev]

    def write(self, payload: bytes, device_index: int, long: bool) -> None:
        if self._closed:
            raise TransportClosed("handle closed")
        frame = H.build_write_frame(device_index, payload, long_message=long)
        if self._frame_len is not None:
            try:
                r = self._dev.write(pad_to(frame, self._frame_len))
            except (OSError, ValueError) as exc:
                raise TransportClosed(f"write error: {exc}") from exc
            if r >= 0:
                return
            # 缓存的长度突然不被接受 → 设备可能被重枚举，判定失效
            raise TransportClosed(f"write rejected at cached size {self._frame_len}")
        n = try_write(self._dev, frame)
        if n is None:
            raise TransportClosed("write rejected at every candidate report size")
        self._frame_len = n
        log.info("output report write size = %d bytes", n)

    def read(self, timeout_s: float) -> bytes | None:
        if self._closed:
            raise TransportClosed("handle closed")
        import time

        deadline = time.monotonic() + timeout_s
        while True:
            for r in self._readers:
                try:
                    data = r.read(64)
                except OSError:
                    continue  # 某个 reader 偶发错误当"暂无数据"；失效由 write 判定
                if data:
                    return bytes(data)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

    def close(self) -> None:
        """关闭句柄：primary + 全部兄弟 collection。

        ⚠️ 2026-10-16 修 bug：本方法**曾经被下面一个同名定义静默覆盖**
        （Python 后定义胜出），那份只关 `self._dev`，于是 `_readers` 里的兄弟
        collection 句柄**永远不会被关闭** —— 每次 `_drop_device()` 都漏一批，
        Windows 上会一直占着设备。现在只剩这一份（覆盖 primary 是因为
        `_readers[0] is self._dev`），**不要再新增同名方法**。
        """
        self._closed = True
        for r in self._readers:
            try:
                r.close()
            except Exception:  # noqa: BLE001 - 关闭失败无所谓
                pass
