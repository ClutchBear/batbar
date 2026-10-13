"""WSL2 侧 controller（用 WSL2 的 uv 环境跑：python + Pillow）。
> ⚠️ **本模块属旧双进程架构的遗留物，已不在主路径**（现为 Windows 单进程自跑）。
> 保留仅为不破坏既有单测；**新功能一律不要引用**。详见 ARCHITECTURE.md §2.3。


职责：
  1. 连上 Windows agent（TCP 客户端），断线自动重连
  2. 用 BatteryPoller 决定何时读一次电量（60s 正常 / 5min 睡眠退避 / 12s 请求超时）
  3. 收到电量 → 在 WSL2 内用 Pillow 渲染电池图标 PNG → base64 推给 agent 换图
  4. agent 主动上报的设备通知（无请求在途）也喂进状态机

设计边界：controller **不 import hid、不 import pystray**——USB 和托盘都在
Windows 侧。因此 WSL2 里 `uv sync --extra controller` 只需 Pillow。

运行（WSL2 内）：
    uv run python -m batbar.controller
    bash run-controller.sh
"""
from __future__ import annotations

import argparse
import logging
import socket
import time

from . import icons, net, wire
from . import hidpp as H
from .state import BatteryPoller, BatteryState

log = logging.getLogger("batbar.controller")

ICON_SIZE = 32
CONNECT_BACKOFF_MAX = 60.0
REQUEST_TIMEOUT = 12.0   # 在途 read 请求超时；agent 硬件读最坏 ~5s×重试，留余量
TICK = 0.5


def _tooltip(state: BatteryState, name: str) -> str:
    if state.info is None:
        return f"{name}：暂无数据（agent 未就绪/鼠标需唤醒）"
    p = state.percent
    base = f"{name}：{p}%" if p is not None else f"{name}：{state.info.approx}"
    if state.info.charging:
        base += " · 充电中"
    if state.sleeping:
        base += " · 休眠"
    return base


class Controller:
    def __init__(self, agent_host: str | None, agent_port: int, device_name: str):
        self._host, self._port, self._name = agent_host, agent_port, device_name
        self._sock: socket.socket | None = None
        self._buf = b""
        self._pending = False       # 有一条 read 请求在途，等它的 battery 回包
        self._pending_ts = 0.0
        self._running = True
        self._poller = BatteryPoller()   # 异步模式：session=None
        self._devices: dict[str, int | None] = {}   # agent 推的设备清单
        self._tracked = "hidpp"                      # 当前跟踪源（与 agent 同步）

    # ---------- 连接 ----------
    def _connect(self) -> bool:
        targets = [f"{self._host}:{self._port}"] if self._host else net.candidate_agents(self._port)
        for spec in targets:
            ip, port = spec.rsplit(":", 1)
            try:
                s = socket.create_connection((ip, int(port)), timeout=3)
            except OSError:
                continue
            s.settimeout(TICK)
            self._sock, self._buf = s, b""
            s.sendall(wire.msg_hello())
            log.info("connected to agent %s", spec)
            return True
        log.warning("agent unreachable, tried: %s", ", ".join(targets))
        return False

    def _drop(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._sock = None
        self._buf = b""

    def _send(self, data: bytes) -> bool:
        if self._sock is None:
            return False
        try:
            self._sock.sendall(data)
            return True
        except OSError:
            self._drop()
            return False

    # ---------- 请求-回包 ----------
    def _request_read(self) -> None:
        if self._send(wire.msg_read()):
            self._pending = True
            self._pending_ts = time.monotonic()
            self._poller.mark_request_sent()

    def _info_from_msg(self, f: wire.Frame) -> H.BatteryInfo:
        return H.BatteryInfo(f.get("percent"), None, bool(f.get("charging")), None,
                             f.get("source") or "agent")

    def _on_battery(self, f: wire.Frame) -> None:
        st = f.get("state", "error")
        if self._pending:
            # 这是我们请求的回包
            self._pending = False
            if st == "ok":
                self._poller.poll_response(self._info_from_msg(f))
            elif st == "sleeping":
                self._poller.poll_response(None, sleeping=True)
            else:
                self._poller.poll_response(None, sleeping=False)
        elif st == "ok":
            # 无请求在途 = 设备主动通知透传
            self._poller.on_notification(self._info_from_msg(f))

    def _handle(self, f: wire.Frame) -> None:
        if f.type == "hi":
            log.debug("agent says hi: %s", f.body)
        elif f.type == "devices":
            self._devices = dict(f.body)
            log.info("device list: %s", self._devices)
        elif f.type == "tracking":
            self._tracked = f.get("device", "hidpp")
            log.info("agent now tracking %s", self._tracked)
        elif f.type == "battery":
            self._on_battery(f)
        elif f.type == "bye":
            self._running = False

    def _recv_tick(self) -> list[wire.Frame]:
        if self._sock is None:
            return []
        out: list[wire.Frame] = []
        try:
            data = self._sock.recv(8192)
        except socket.timeout:
            return out
        except OSError:
            self._drop()
            return out
        if not data:
            log.info("agent closed connection")
            self._drop()
            return out
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            fr = wire.decode_line(line)
            if fr:
                out.append(fr)
        return out

    # ---------- 图标推送 ----------
    def _display_name(self) -> str:
        if self._tracked == "hidpp":
            return self._name
        if self._tracked.startswith("BT:"):
            return self._tracked[3:]
        return self._tracked

    def _push_icon(self, state: BatteryState) -> None:
        charging = bool(state.info and state.info.charging)
        spec = icons.IconSpec(state.percent, charging, ICON_SIZE)
        png = icons.render_icon(spec)
        self._send(wire.msg_icon(png, _tooltip(state, self._display_name()), state.percent, charging))

    # ---------- 主循环 ----------
    def run(self) -> None:
        backoff = 2.0
        while self._running:
            if self._sock is None:
                if self._connect():
                    backoff = 2.0
                else:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, CONNECT_BACKOFF_MAX)
                    continue

            if self._poller.request_due():
                self._request_read()

            if self._pending and time.monotonic() - self._pending_ts > REQUEST_TIMEOUT:
                log.debug("read request timed out")
                self._pending = False
                self._poller.note_timeout()

            for f in self._recv_tick():
                self._handle(f)

            st = self._poller.take_icon_update()
            if st is not None:
                self._push_icon(st)

            time.sleep(TICK)


def main():
    ap = argparse.ArgumentParser(prog="batbar-controller")
    ap.add_argument("--agent-host", default=None, help="留空自动探测（mirrored/NAT 自适应）")
    ap.add_argument("--agent-port", type=int, default=net.DEFAULT_PORT)
    ap.add_argument("--device-name", default="鼠标 罗技 G304", help="托盘 tooltip 显示名")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Controller(args.agent_host, args.agent_port, args.device_name).run()


if __name__ == "__main__":
    main()
