"""WSL2 controller ↔ Windows agent 的桥接协议（行分隔 JSON，纯逻辑可单测）。
> ⚠️ **本模块属旧双进程架构的遗留物，已不在主路径**（现为 Windows 单进程自跑）。
> 保留仅为不破坏既有单测；**新功能一律不要引用**。详见 ARCHITECTURE.md §2.3。


为什么是这套：WSLg 不把 Linux 托盘导出到 Windows，且 usbipd 把接收器 attach
进 WSL2 会让 Windows 当场失去鼠标（独占转移）。所以——

  controller（WSL2 内跑，用 WSL2 的 uv/Python + Pillow）
      │  决定轮询节奏、渲染电池图标、维护 tooltip 文案
      │  不碰 USB、不碰托盘
      ▼  TCP（newline-delimited JSON）
  agent（Windows 原生 Python 跑）
         打开 HID++ vendor collection 读电量 + 用 pystray 贴图标

方向固定为 controller→agent：WSL2 能拿到 Windows host 的 vEthernet IP，
WSL→Windows 的 localhost 连接稳定；反向（Windows 连 WSL2 NAT 内的端口）要靠
额外端口转发，不划算。因此 **agent 是 server，controller 是 client**。

协议消息（都带 "t" 字段表类型）：
  controller → agent
    {"t":"hello","v":"0.1"}
    {"t":"read","devnum":null}            # 请求电量；agent 命中 TTL 缓存可不真读
    {"t":"icon","png":"<base64>","tooltip":"...","percent":85,"charging":false}
    {"t":"quit"}
  agent → controller
    {"t":"hi","platform":"win","hid":true}
    {"t":"battery","source":"0x1004","percent":72,"charging":false,"state":"ok|sleeping|error"}
    {"t":"ack","ok":true}
    {"t":"err","msg":"..."}
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

PROTO_VERSION = "0.1"


@dataclass
class Frame:
    """一条已解码的桥接消息。type=顶层 't' 字段，body=其余字段。"""
    type: str
    body: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)


def encode_msg(t: str, **fields: Any) -> bytes:
    """把消息编码为一行 JSON + '\n'。bytes 字段需调用方先 base64。"""
    obj = {"t": t, **fields}
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decode_line(line: bytes | str) -> Frame | None:
    """解码一行；空行/坏行返回 None（调用方应跳过而非崩溃）。"""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "t" not in obj:
        return None
    t = obj.pop("t")
    return Frame(type=str(t), body=obj)


def b64_png(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")


def un_b64_png(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def msg_hello() -> bytes:
    return encode_msg("hello", v=PROTO_VERSION)


def msg_read(devnum: int | None = None) -> bytes:
    return encode_msg("read", devnum=devnum)


def msg_switch(device: str) -> bytes:
    """controller → agent：切换托盘图标当前显示的设备。

    device 取值："hidpp"（罗技接收器）或 "BT:<设备名>"（蓝牙，名来自 devices 消息）。"""
    return encode_msg("switch", device=device)


def msg_icon(png: bytes, tooltip: str, percent: int | None, charging: bool) -> bytes:
    return encode_msg("icon", png=b64_png(png), tooltip=tooltip, percent=percent, charging=charging)


def msg_quit() -> bytes:
    return encode_msg("quit")
