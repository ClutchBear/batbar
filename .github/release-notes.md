## 下载

**`batbar-*-win64.zip`** —— 解压即用，**不需要装 Python**，不需要任何运行时。

1. 解压到任意位置（例如 `D:\batbar\`）
2. 双击 `batbar.exe` —— 它会常驻系统托盘（**没有窗口，属正常**）
3. 退出：托盘图标右键 → 退出

压缩包里还附了 `使用说明.txt`，含配置项与故障排查。

## 本版变化（v0.1.4）

**把 v0.1.3 里「只删了一半」的旧架构彻底清干净。**

上一版删掉了 `agent.py` 里的监听代码，但「双进程 + 本地 TCP 桥」那套的
另外三个文件还留在仓库里：

| 删掉的文件 | 原职责 |
|---|---|
| `wire.py` | controller ↔ agent 的桥接协议（行分隔 JSON） |
| `net.py` | 网络定位辅助（查本机 IP 给 controller 连） |
| `controller.py` | 外部调度器 + 渲染 + TCP 客户端 |

`controller.py` 是死代码 —— agent 已经不监听了，它永远连不上；`wire.py`
和 `net.py` 只是为它服务。留着它们的唯一理由是「不破坏既有单测」，于是就
出现了「为死代码写测试、测试又反过来保住死代码」的死循环。一并删掉，连带
`test_framing.py` 与 `test_wire_state.py` 里的 wire 部分
（`test_state.py` 里覆盖 `state.py` 的 12 项保留）。

现在整个包**不 import socket、不开任何端口**：batbar 读电量走的是本机 HID，
全程不联网 —— 这点是**在真机上验过的**，不是推断（见下）。

### 上一版（v0.1.3）：不再弹防火墙

删掉了旧架构遗留的本地 TCP 监听（`0.0.0.0:51788`）。这个端口早期用于
「Windows 侧 agent + WSL2 侧 controller」双进程通信，改单进程自跑后
controller 废弃，监听却留在 `agent.py` 里没一起删 ⇒ 每换一台机器、第一次
双击 exe，防火墙都要问一次「是否允许 batbar.exe 访问网络」。现在进程起来后
**零监听端口**，功能零影响。

### 上上版（v0.1.2）：改成绿色（便携）模式

数据不再写进 `%LOCALAPPDATA%\batbar`，而是跟 `batbar.exe` 放在同一个目录：
解压到哪，数据就在哪；整个目录拷走即可迁移。

```
D:\batbar\
├── batbar.exe
├── logs\agent.log       ← 日志
├── last-known.json      ← 上次电量（重启后先显示旧值，不等首次读数）
└── agent.pid            ← 供重启脚本秒杀旧实例
```

- 首次启动会把旧 `%LOCALAPPDATA%` 里的历史数据**复制**过来（只复制、不删源、
  不覆盖已有文件）。此后新数据一律只写在 exe 旁边。
- 若 exe 所在目录不可写（例如解压到 `Program Files`），自动回退到
  `%LOCALAPPDATA%\batbar`，并在日志里写明回退原因 —— 不会因为写不进去就不落盘。
- 想指定别处：设环境变量 `BATBAR_DATA_DIR=<目录>`。
- 升级方式：解压新包覆盖 exe，把旧的 `logs\`、`last-known.json` 一起放进去即可。

## 系统要求

Windows 10 / 11 64 位。

## 这个包是怎么来的

由 GitHub Actions 从本 tag 的源码在 `windows-latest` 上自动构建：

```
python -m pip install -e ".[agent,dev]" pyinstaller
python -m pytest
python -m PyInstaller --noconfirm --clean batbar.spec
```

`dist\batbar.exe` + `LICENSE` + `使用说明.txt` 打包成 zip —— **恰好这三个文件**。
CI 会拿发布目录做「真跑 exe 前后必须一模一样」的比对，所以 `logs\`、
`last-known.json`、`agent.pid` 这些运行期产物不会被夹带进包里。
构建步骤见仓库的
[`.github/workflows/build-exe.yml`](https://github.com/ClutchBear/batbar/blob/main/.github/workflows/build-exe.yml)，
本地打包用 `build-exe.bat`，两条路径的产物一致。

## 许可与归属

MIT，Copyright (c) 2026 ClutchBear。

第三方依赖：cython-hidapi（BSD-3-Clause）、pystray（LGPL-3.0）、Pillow（MIT-CMU）。
