"""HidppSession：对单个罗技设备（接收器对码设备 / BT 直连）的 HID++ 会话。

transport-agnostic —— 真正的 USB 读写由注入的 transport 完成（Windows 侧是
hidapi，单测侧是 FakeTransport），本模块只做协议编排：

  1. find_feature(num)：ROOT.GetFeatures 把「特性号」（0x1004）翻成「特性索引」
  2. read_battery()：按特性优先级逐个尝试，返回 BatteryInfo
  3. 错误处理：Solaar base.py:672-800 那套 request/匹配/超时/错误帧逻辑的极简版

与 Solaar 的差异（KISS）：
  - 不做接收器多设备扫描（本机 C53F 只挂一个鼠标；devnum 默认 0xFF=BT/直连，
    接收器对码设备由配置传 1-6）
  - 通知不单独开线程；read_battery 的读循环里顺手消化通知帧（见 _pump）
"""

from __future__ import annotations

import logging
import struct
import threading
from dataclasses import dataclass

from . import hidpp as H

log = logging.getLogger("batbar.session")

# 电池特性尝试顺序（现代鼠标优先；对齐 Solaar battery_functions 的可用性覆盖）
BATTERY_CANDIDATES = [
    (H.FEAT_UNIFIED_BATTERY, H.parse_unified_battery),
    (H.FEAT_BATTERY_STATUS, H.parse_battery_status),
    (H.FEAT_BATTERY_VOLTAGE, H.parse_battery_voltage),
    (H.FEAT_ADC_MEASUREMENT, H.parse_adc_measurement),
]


class TransportClosed(Exception):
    """设备句柄失效（拔线/睡眠后没醒来/被 detach）。"""


class Transport:
    """需要 Windows 侧（或单测 Fake）实现的最小读写接口。子类必须覆写全部方法。"""

    def write(self, payload: bytes, device_index: int, long: bool) -> None:
        raise NotImplementedError

    def read(self, timeout_s: float) -> bytes | None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class HidppSession:
    def __init__(
        self, transport: Transport, device_index: int = H.DEV_RECEIVER, timeout: float = 0.6, retries: int = 1
    ):
        self._t = transport
        self._devnum = device_index
        self._timeout = timeout  # 单次请求超时；扫描空槽位时可调小加速
        self._retries = retries
        # RLock：read_battery → find_feature → _request 嵌套加锁，必须可重入
        self._lock = threading.RLock()
        self._sw = 0
        self._feat_cache: dict[int, int | None] = {}  # feature_num -> index（None=不支持）
        self._battery_feat: tuple[int, int] | None = None  # (num, index)

    # ---------- 底层请求 ----------
    def _next_sw(self) -> int:
        # Solaar base.py:696 约定：sw_id 恒带 bit3（原版 `0x08 | getrandbits(3)`，
        # 新版固定 0x0B）。这样「请求回复」的回显永远能与设备通知区分开。
        self._sw = self._sw % 7 + 1
        return self._sw | 0x08

    def _request(
        self, request_id: int, params: bytes = b"", timeout: float | None = None, retries: int | None = None
    ) -> bytes | None:
        """发送一个 HID++ 请求并等待匹配回复。返回 payload[2:]（数据段）或 None。

        timeout/retries 不传时用实例默认值（__init__ 的 self._timeout/_retries），
        扫描空槽位等场景可在构造时调小以加速。"""
        if timeout is None:
            timeout = self._timeout
        if retries is None:
            retries = self._retries
        with self._lock:
            for _ in range(retries + 1):
                sw = self._next_sw()
                rid = (request_id & 0xFFF0) | sw
                payload = struct.pack("!H", rid) + params
                try:
                    self._t.write(payload, self._devnum, len(payload) > H.SHORT_DATA)
                except TransportClosed:
                    raise
                got = self._await_reply(rid, payload[:2], timeout)
                if got is not None:
                    return got
            return None

    def _await_reply(self, rid: int, head: bytes, timeout: float) -> bytes | None:
        """读循环：匹配 request 头两字节；消化通知；识别错误帧。

        帧语义对齐 Solaar base.request（base.py:725-781）：
          正常回复  payload[:2] == request_id 回显（含 sw_id）
          HID++1.0 错误 payload[0]==0x8F 且 payload[1:3]==request_id
          HID++2.0 错误 payload[0]==0xFF 且 payload[1:3]==request_id
          BT 设备回复 devnum 回 0x00（发送 0xFF）——parse_input_report 不校验 devnum，
          匹配只靠 payload 头两字节，天然兼容。
        """
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            raw = self._t.read(max(0.05, deadline - _time.monotonic()))
            if raw is None:
                continue
            parsed = H.parse_input_report(raw)
            if parsed is None:
                continue
            _rid, _devnum, payload = parsed
            err = H.frame_error(payload)
            if err is not None:
                # 错误帧回显 request_id 在 payload[1:3]（正常帧在 payload[0:2]）
                if payload[1:3] == head[:2]:
                    log.debug("feature error 0x%02X on %04X", err, rid)
                    return None
                continue
            if payload[:2] == head[:2]:
                return payload[2:]
            if H.is_notification(payload):
                self._on_notification(payload)
        return None

    def _on_notification(self, payload: bytes) -> None:
        """通知帧回调钩子（由 agent 注入以刷新缓存）。默认吞掉。"""
        if self._battery_feat and self._notify_cb:
            featnum, featidx = self._battery_feat
            info = H.parse_notification_battery(payload, featidx, featnum)
            if info is not None:
                self._notify_cb(info)

    _notify_cb = None

    def set_notification_callback(self, cb) -> None:
        self._notify_cb = cb

    # ---------- 特性发现 ----------
    def find_feature(self, feature_num: int) -> int | None:
        """ROOT.GetFeatures(function 0x00, 参数=feature 号大端 2 字节) → 回复字节 0 = 索引。
        见 Solaar hidpp20.py FeaturesArray._check 与 GetFeatures 用法。"""
        if feature_num in self._feat_cache:
            return self._feat_cache[feature_num]
        reply = self._request(H.FEATURE_ROOT << 8 | 0x00, struct.pack("!H", feature_num))
        # 注意：ROOT 页索引恒 0，request 高字节 = 0
        index = reply[0] if reply and len(reply) >= 1 else None
        self._feat_cache[feature_num] = index
        return index

    def protocol_version(self) -> float | None:
        """向 device_index 发 PING（ROOT fn 0x01）。
        回复 = [协议 major, minor]（如 4.2）；接收器自己回 1.0。
        对端不存在/在睡 → 超时 None。"""
        reply = self._request(0x0010, bytes([0x00]), retries=0, timeout=0.8)
        if reply and len(reply) >= 2:
            return reply[0] + reply[1] / 10.0
        return None

    # 语义别名：ping 一下 = 探活并取协议版本；None 表示对端睡着/不存在
    ping = protocol_version

    # ---------- HID++ 1.0 寄存器（接收器侧状态，Unifying 规范 §4） ----------
    def read_register(
        self, register: int, device_index: int | None = None, timeout: float = 0.6
    ) -> bytes | None:
        """读 HID++ 1.0 短寄存器（sub_id 0x81）。

        请求帧 = [0x81, register, 0x00, 0x00, 0x00]；回复 =
        [0x81, register, r0, r1, r2] → 返回 [r0, r1, r2]；无应答/错误帧 → None。

        ⚠️ **不能走 `_request`**：它按 HID++ 2.0 feature call 约定把
        request_id 低字节掩进 sw_id（`rid & 0xFFF0 | sw`），而寄存器请求的
        第二个字节**就是寄存器号**（规范原文：读 Connection State 发
        `10 FF 81 02 00 00 00`），掩一下寄存器号就没了。这里按规范原样
        拼帧、按前两字节回显匹配（回复必回显 [0x81, register]）。
        """
        with self._lock:
            payload = bytes([0x81, register & 0xFF, 0x00, 0x00, 0x00])
            target = self._devnum if device_index is None else device_index
            self._t.write(payload, target, False)
            return self._await_reply(0x8100 | (register & 0xFF), payload[:2], timeout)

    # ⚠️ 寄存器 0x02「Connection State」判不了「设备关机没」（2026-10-13
    # 实测：Bolt C53F 上鼠标关机后已连接数也不掉，更像「对码/挂接数」），
    # 曾经的 connection_state() 判据因此整体移除。判断在/离线请用
    # `read_battery()` 应不应答（空闲 40s 的 G304 依然 0.20s 内应答，见
    # `agent._read_with_second_chance`：两次不应答 = 不在）。
    def battery_feature(self) -> tuple[int, int] | None:
        """惰性发现并缓存 (特性号, 特性索引)。"""
        if self._battery_feat is None:
            for featnum, _parse in BATTERY_CANDIDATES:
                idx = self.find_feature(featnum)
                if idx is not None and idx != 0:
                    self._battery_feat = (featnum, idx)
                    break
        return self._battery_feat

    # ---------- 电池 ----------
    def read_battery(self) -> H.BatteryInfo | None:
        bf = self.battery_feature()
        if bf is None:
            log.warning("device has no known battery feature")
            return None
        featnum, featidx = bf
        parse = dict(BATTERY_CANDIDATES)[featnum]
        if featnum == H.FEAT_UNIFIED_BATTERY:
            reply = self._request(featidx << 8 | 0x10)  # Solaar: function 0x10 读快档
        elif featnum == H.FEAT_ADC_MEASUREMENT:
            reply = self._request(featidx << 8 | 0x10)  # GetConfiguration，失败退回 0x00
            if reply is None or len(reply) < 3:
                reply = self._request(featidx << 8)
        else:
            reply = self._request(featidx << 8)
        if not reply:
            return None
        info = parse(reply)
        if info is not None and info.percent is not None and not info.charging:
            # 电压类特性只给电压时按 Solaar 折点表已折算；档位设备补 approx
            pass
        return info

    def close(self) -> None:
        self._t.close()
