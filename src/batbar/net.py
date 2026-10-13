"""WSL2 ↔ Windows 网络定位工具（纯标准库）。
> ⚠️ **本模块属旧双进程架构的遗留物，已不在主路径**（现为 Windows 单进程自跑）。
> 保留仅为不破坏既有单测；**新功能一律不要引用**。详见 ARCHITECTURE.md §2.3。


WSL2 连 Windows 侧 agent 的三种现实：
  1. mirrored 网络模式（WSL ≥2.4.4 + Win11）：localhost 双向直达，直接连 127.0.0.1
  2. NAT 模式（默认）：Windows host = 默认网关 IP；WSL→Windows 可达（agent 绑 0.0.0.0），
     但 Windows 防火墙可能拦，需要放行规则（见 setup 脚本）
  3. 老镜像/自定义：两路都试，失败给出明确诊断

本模块在 WSL2 内用 `ip route` 拿默认网关；非 WSL 环境返回 None。
"""
from __future__ import annotations

import os
import subprocess

DEFAULT_PORT = 51788  # agent 监听端口（controller 默认连这个）


def is_wsl() -> bool:
    return os.environ.get("WSL_DISTRO_NAME") is not None or "microsoft" in os.uname().release.lower()


def windows_host_ip() -> str | None:
    """WSL2 NAT 模式下 Windows host 的 IP（默认网关）。mirrored 模式下无意义。"""
    if not is_wsl():
        return None
    try:
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=3)
        for tok in out.stdout.split():
            if tok[0].isdigit() and "." in tok:
                return tok
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def candidate_agents(port: int) -> list[str]:
    """按优先级返回要试的 agent 地址。mirrored 先试 localhost，NAT 再试网关。"""
    cands = ["127.0.0.1"]
    gw = windows_host_ip()
    if gw and gw not in cands:
        cands.append(gw)
    return [f"{ip}:{port}" for ip in cands]
