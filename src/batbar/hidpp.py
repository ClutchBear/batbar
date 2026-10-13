"""HID++ 2.0 协议编解码（纯 bytes 逻辑，零依赖，可离线单测）。

所有帧布局与常量均以 Solaar 项目源码为准（pwr-Solaar/Solaar master 分支
lib/logitech_receiver/{base,hidpp20,hidpp20_constants,common}.py），
关键出处在注释里标了行号级说明。

两套设备形态，帧前缀不同：

1) Bolt/Unifying 接收器（本机实测 = VID_046D&PID_C53F，MI_02 是 vendor
   collection）。写帧 = [report_id, device_index] + payload，payload =
   [sw_id|request_hi, request_lo, params...]。
   - 短帧 report_id 0x10，payload 补零到 6 字节（总 8 字节）
   - 长帧 report_id 0x11，payload 补零到 19 字节（总 21 字节）
   device_index：1-6 = 对码设备，0xFF = 接收器自身。

2) 蓝牙直连 / 有线 HID++ 设备：没有 device_index 这一层，
   HID++ 消息直接从 sw_id 字节开始，写帧 = [0x01, sw_id, sw_id, ...]
   （Linux hidraw 下首个 0x01 是 output report id；Windows hidapi 用
   write(bytes([0x00]) + msg) 让 hidapi 自己填）。

sw_id：取 1..7（bit3 恒置位）。Solaar base.py 用 `request_id & 0xFFF0 | sw_id`，
bit3=1 用于把「请求回复」和「设备主动通知」区分开；通知的 sw_id 字节 bit3=0。

request_id 语义：
  - HID++ 1.0 寄存器访问：0x81xx / 0x82xx / 0x83xx（read/write/getreg 等）
  - HID++ 2.0 feature call：request_id = (feature_index << 8) | function_sw_id
    其中 function_sw_id 的 bit4..7 存功能号（0x00/0x10/0x20...），低 4 位被
    sw_id 覆盖。发送时 request_id 拆成大端两字节。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# ---- 帧常量（Solaar base.py:95-96,449-456）----
REPORT_ID_SHORT = 0x10
REPORT_ID_LONG = 0x11
# 帧 = [report_id, device_index, data]；data 短帧定长 5 字节、长帧 18 字节。
# data = request_id(2B) + params(≤3B 短帧 / ≤16B 长帧)。总长 7B / 20B。
SHORT_DATA = 5
LONG_DATA = 18

DEV_RECEIVER = 0xFF  # 寻址到接收器本身
BOLT_PID = 0xC53F    # 本机实测 Bolt 接收器 PID

# ---- HID++ 2.0 核心特性页（Solaar hidpp20_constants.py:33-34）----
FEATURE_ROOT = 0x0000
FEATURE_FEATURE_SET = 0x0001

# ROOT 页功能号
FN_ROOT_GET_FEATURE = 0x00   # 参数：目标 feature 号（大端 2 字节）→ 回复字节 0 = 索引
FN_ROOT_PING = 0x01          # 参数：ping 字节
FN_ROOT_GET_FEATURE_COUNT = 0x02  # → 回复字节 0 = 特性个数（不含 ROOT）

# ---- 电池特性页（Solaar hidpp20_constants.py:53-55,90）----
FEAT_BATTERY_STATUS = 0x1000   # 早期，档位粗
FEAT_BATTERY_VOLTAGE = 0x1001  # 电压
FEAT_UNIFIED_BATTERY = 0x1004  # 现代鼠标主力（MX Anywhere/Master 系列）
FEAT_ADC_MEASUREMENT = 0x1F20  # 充电状态机


def encode_feature_request(feature_index: int, function: int, sw_id: int, params: bytes = b"") -> bytes:
    """编码 HID++ 2.0 feature call 的 payload（不含 report_id/device_index 前缀）。

    Args:
        feature_index: 特性在设备特性表中的索引（不是特性号！先用 get_feature_index 查）
        function: 功能号，占高 nibble（0x00/0x10/0x20...）
        sw_id: 1..7
        params: 参数字节，payload 总长（1+1+len(params)）≤ 6，超长须走长帧

    Returns:
        payload = struct.pack("!H", request_id) + params，即
        [feature_index_hi, function_hi_nibble | sw_id, *params]。
        sw_id 落在 request_id 低字节的低 nibble（Solaar base.py 的
        `(request_id & 0xFFF0) | sw_id`）。
    """
    assert 1 <= sw_id <= 7, sw_id
    request_id = (feature_index << 8) | (function & 0xF0)
    request_id = (request_id & 0xFFF0) | sw_id
    payload = struct.pack("!H", request_id) + params
    return payload


def decode_feature_reply(payload: bytes) -> bytes:
    """从 feature call 的回复 payload 中提取数据段。

    回复 payload 布局: [sw_id|feat_hi, feat_lo, data...]（短帧）；
    错误回复: [0x8F, feat_hi, feat_lo, error]（HID++1.0 错误）或
              [0xFF, feat_hi, feat_lo, error]（HID++2.0 错误）。

    Args:
        payload: 帧头（report_id, devnum）之后的数据
    Returns:
        数据段 payload[2:]；若是错误帧则返回 b""（上层按 None 处理）。
    """
    if payload[:1] in (b"\x8f", b"\xff"):
        return b""
    return payload[2:]


def build_write_frame(device_index: int, payload: bytes, long_message: bool = False) -> bytes:
    """接收器模式：拼完整写帧 [report_id, device_index, data(5|18B)]。

    cython-hidapi Windows 后端：write() 首字节即 output report id 并被剥掉，
    所以直接把 0x10/0x11 放首字节。data 定长补零（Solaar base.py:449-456 的
    `struct.pack("!BB5s"/"!BB18s", rid, devnum, data)`）。

    payload = encode_feature_request 的输出（sw_id 起），≤5 字节走短帧。
    """
    rid = REPORT_ID_LONG if long_message else REPORT_ID_SHORT
    target = LONG_DATA if long_message else SHORT_DATA
    payload = payload[:target].ljust(target, b"\x00")
    return struct.pack("!BB", rid, device_index) + payload


@dataclass(frozen=True)
class BatteryInfo:
    """归一化的电池读数。level 单位 %，None 表示设备只给档位不给精确值。"""
    level: int | None       # 0-100 或 None
    approx: str | None      # "empty|critical|low|good|full" 之一
    charging: bool
    voltage_mv: int | None
    source: str             # "0x1004"/"0x1000"/"0x1001"/"0x1F20"/"bluetooth"/"unified"

    @property
    def percent(self) -> int | None:
        if self.level is not None:
            return self.level
        # 只有档位时的展示值（与 Solaar BatteryLevelApproximation 一致：0/5/20/50/90）
        m = {"empty": 0, "critical": 5, "low": 20, "good": 50, "full": 100}
        return m.get(self.approx or "", None)

    def is_charging(self) -> bool:
        return self.charging


# BatteryStatus 枚举值（Solaar common.py:640-649）
STATUS_DISCHARGING = 0x00
STATUS_RECHARGING = 0x01
STATUS_ALMOST_FULL = 0x02
STATUS_FULL = 0x03
STATUS_SLOW_RECHARGE = 0x04

# BatteryLevelApproximation 档位映射（Solaar common.py:652-657 + hidpp20.py:2254-2263）
_UNIFIED_LEVELS = {8: "full", 4: "good", 2: "low", 1: "critical", 0: "empty"}


def parse_battery_status(reply: bytes) -> BatteryInfo | None:
    """0x1000 BATTERY_STATUS。reply 前 3 字节 = [level%, next%, status]（hidpp20.py:2191）。"""
    if len(reply) < 3:
        return None
    level, nxt, status = struct.unpack("!BBB", reply[:3])
    approx = None
    if level == 0:
        level = None
        # 0x1000 设备通常只报档位；Solaar 也是按状态推断
    return BatteryInfo(level, approx, status in (STATUS_RECHARGING, STATUS_ALMOST_FULL, STATUS_FULL, STATUS_SLOW_RECHARGE), None, "0x1000")


def parse_battery_voltage(reply: bytes) -> BatteryInfo | None:
    """0x1001 BATTERY_VOLTAGE。前 3 字节 = [voltage_hi, voltage_lo, flags]（hidpp20.py:2207）。"""
    if len(reply) < 3:
        return None
    voltage, flags = struct.unpack(">HB", reply[:3])
    charging = bool(flags & (1 << 7))
    pct = estimate_voltage_percent(voltage)
    return BatteryInfo(pct, None, charging, voltage, "0x1001")


def parse_unified_battery(reply: bytes) -> BatteryInfo | None:
    """0x1004 UNIFIED_BATTERY。前 4 字节 = [discharge%, level_byte, status, ignore]（hidpp20.py:2244）。"""
    if len(reply) < 4:
        return None
    discharge, level_byte, status_byte, _ = struct.unpack("!BBBB", reply[:4])
    approx = _UNIFIED_LEVELS.get(level_byte & 0x0F)
    status_byte &= 0x0F
    charging = status_byte in (STATUS_RECHARGING, STATUS_ALMOST_FULL, STATUS_FULL)
    return BatteryInfo(discharge if discharge else None, approx, charging, None, "0x1004")


def parse_adc_measurement(reply: bytes) -> BatteryInfo | None:
    """0x1F20 ADC_MEASUREMENT。前 3 字节 = [adc_hi, adc_lo, flags]（hidpp20.py:2271）。
    flags bit0=有效, bit1=充电。"""
    if len(reply) < 3:
        return None
    adc, flags = struct.unpack(">HB", reply[:3])
    if not flags & 0x01:
        return None
    charging = bool(flags & 0x02)
    pct = estimate_voltage_percent(adc)
    return BatteryInfo(pct, None, charging, adc, "0x1F20")


def estimate_voltage_percent(mv: int) -> int | None:
    """电压→百分比线性插值。折点表抄自 Solaar hidpp20.py:2290-2304。"""
    table = [
        (4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60),
        (3811, 50), (3778, 40), (3751, 30), (3717, 20), (3671, 10),
        (3646, 5), (3579, 2), (3500, 0),
    ]
    if mv >= table[0][0]:
        return table[0][1]
    if mv <= table[-1][0]:
        return table[-1][1]
    for (v_hi, p_hi), (v_lo, p_lo) in zip(table, table[1:]):
        if v_lo <= mv <= v_hi:
            return int(round(p_lo + (p_hi - p_lo) * (mv - v_lo) / (v_hi - v_lo)))
    return None


# ------------------------------------------------------------------
# 输入帧解析
# ------------------------------------------------------------------
# 罗技 vendor collection 的描述符把 0x10/0x11 声明成真正的 HID Report ID：
#   Windows hidapi：write 首字节当 Report ID 并被剥掉 → 我们直接发
#       [0x10, devnum, ...] 即可（与 Solaar 同一代码路径）；read 返回首字节 = Report ID。
#   Linux hidraw：write 原样发送、read 带 Report ID 首字节。
# 两端行为一致，但历史版本/hidapi 实现偶有不带 ID 的情况，故解析层做容错。

def parse_input_report(raw: bytes) -> tuple[int, int, bytes] | None:
    """把设备读回的原始字节解析为 (report_id, device_index, payload)。

    罗技 vendor collection 的输入帧首字节就是 report id（0x10/0x11），hidapi
    原样返回；个别 hidapi 构建/版本会剥掉首字节，此时 raw = [devnum, data...]，
    短帧 6 字节、长帧 19 字节，用长度回推 report id。

    Args:
        raw: hidapi/hidraw 读回的完整字节（7/20 字节，Bolt 也可能 64）
    Returns:
        None 表示无法识别；payload = [feat_idx, fn|swid, data...]（不截断，
        数据可能比声明长度长——交给各 parse 函数取所需前缀）。
    """
    if not raw or len(raw) < 3:
        return None
    if raw[0] in (REPORT_ID_SHORT, REPORT_ID_LONG):
        return raw[0], raw[1], raw[2:]
    # 无 report-id 前缀的变体：首字节 = device_index
    rid = REPORT_ID_LONG if len(raw) >= 8 else REPORT_ID_SHORT
    return rid, raw[0], raw[1:]


def frame_error(payload: bytes) -> int | None:
    """检测错误帧。返回错误码（HID++1.0 错误 [0x8F,hi,lo,err]；2.0 错误 [0xFF,hi,lo,err]），
    非错误帧返回 None。见 Solaar base.py:732-767。"""
    if payload[:1] == b"\x8f" and len(payload) >= 4:
        return payload[3]
    if payload[:1] == b"\xff" and len(payload) >= 4:
        return payload[3]
    return None


def is_notification(payload: bytes) -> bool:
    """Solaar base.py:692-696 约定：请求帧 request_id 低 nibble = sw_id ∈ 0x08..0x0F
    （恒置 bit3，便于把「请求回复」和「设备通知」区分开）。
    回复回显 request_id → payload[1] 低 nibble bit3=1；
    通知帧低 nibble 不遵循 sw_id 约定（bit3=0）。payload[0] 是 feature 索引，与 sw_id 无关。"""
    return bool(payload) and len(payload) >= 2 and (payload[1] & 0x0F) < 0x08


def frame_feature_index(payload: bytes) -> int:
    """回复/通知帧的 feature index = payload[0]（request_id 高字节原样回显，
    Solaar base.py:769 用 reply_data[:2] == request_data[:2] 匹配请求）。"""
    return payload[0]


def frame_function(payload: bytes) -> int:
    """帧的功能号 = payload[1] 的高 nibble。"""
    return payload[1] & 0xF0


def parse_notification_battery(payload: bytes, battery_feature_index: int, battery_feature_num: int) -> BatteryInfo | None:
    """设备主动上报电池（省电时它推、我们不用轮询）。

    通知帧布局与查询回复相同：[feat_index, function|0, data...]。
    只有 feature index 命中电池特性页、且 function=0（电池状态上报）才解析，
    避免把其它特性页通知误读成电池。
    """
    if len(payload) < 4:
        return None
    if frame_feature_index(payload) != battery_feature_index:
        return None
    if frame_function(payload) != 0:
        return None
    data = payload[2:]
    if battery_feature_num == FEAT_UNIFIED_BATTERY:
        return parse_unified_battery(data)
    if battery_feature_num == FEAT_BATTERY_STATUS:
        return parse_battery_status(data)
    if battery_feature_num == FEAT_BATTERY_VOLTAGE:
        return parse_battery_voltage(data)
    if battery_feature_num == FEAT_ADC_MEASUREMENT:
        return parse_adc_measurement(data)
    return None
