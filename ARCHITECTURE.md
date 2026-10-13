# ARCHITECTURE.md —— batbar 架构地图与 AI 维护指南

> 本文件是 batbar 的**代码认知地图**与**修改入口索引**。
> README.md 讲「为什么这么设计 / 怎么跑起来 / 踩过哪些坑」（探索史很长，1400+ 行）；
> **本文件只回答两个问题：某个文件负责什么？想改 X 该动哪个文件？**
>
> 参考形态借鉴自同类项目 FerrisPulse（灵脉）的 `ARCHITECTURE.md`——它的核心价值
> 是把「模块职责」和「功能路由」各做成一张表，让改动前不必通读全仓。

---

## 1. 单进程架构（一图看全）

```
┌────────────────────── Windows 原生进程（单进程全包）──────────────────────┐
│  agent.py  (--standalone 轮询→渲染→托盘 / --probe 自检 / --scan 诊断)     │
│    ├── 唯一被允许 import hid 的进程（transport.py → HID++ vendor 通道）   │
│    ├── state.BatteryPoller 轮询节奏 / 睡眠退避 / 变化检测 / 多源仲裁       │
│    ├── icons.py Pillow 画百分比数字 → PNG bytes                           │
│    └── pystray 贴托盘图标 + 右键菜单（切换显示哪台设备）                   │
│  数据来源（互不干扰，各自有判据）：                                        │
│    ① HID++ 直连（罗技鼠标：接收器/有线/BT） → session.py / hidpp.py      │
│    ② 2.4G 接收器被动监听 + 轮询       → dongle.py / poll24.py           │
│    ③ 2.4G 私有接收器识别 + 有线兜底    → receiver24.py / wired24.py      │
│    ④ 蓝牙 HID 设备（耳机/键盘）        → bt_battery.py                    │
│    ⑤ 设备/任务栏事件（插拔、explorer）  → winevents.py（消息窗口，非轮询） │
│    ⑥ 鼠标活动唤醒（RAWINPUT/INPUTSINK） → winevents.py 解码 + agent 的窗口 │
│    ⑦ 全屏/游戏态（QUNS）→ 静音提醒+暂停轮询 → fullscreen.py（纯逻辑门控） │
│  Win32 底座：devices 枚举/通知 全走     → win32ctypes.py（ctypes 唯一真相源）│
└───────────────────────────────────────────────────────────────────────────┘
```

**为什么是单进程**：读 USB、画图、贴托盘三件事都在 Windows 侧，中间不需要任何 IPC。
早期版本曾把「计算与渲染」拆到另一个进程用本地 TCP 桥相连（`controller.py`），实测
**没有收益** —— Pillow 画一张 32×32 PNG 是微秒级，跨进程序列化反而多一层故障面。

**分层原则（仍然保留，是单测可跑的前提）**：
`hidpp/session/state/lowbattery/lastknown/winevents/fullscreen` 零第三方依赖，
`transport.py` 是唯一 `import hid` 的文件 —— 这样 502 项单测**无需任何硬件**就能全绿。

> 旧双进程架构的三件套（`wire.py` / `net.py` / `controller.py`）已于 v0.1.4 整套删除。
> 历史沿革见 §8；**不要**从 git 历史里把它们捞回来（守门测试见 §5 铁律 23）。

---

## 2. 模块认知地图（每个文件一句话职责）

### 2.1 纯逻辑层（零第三方依赖，任意平台可离线单测）★

| 文件 | 行数 | 唯一职责 | 关键约束 |
|---|---:|---|---|
| `hidpp.py` | 293 | HID++ 2.0 **帧编解码**：帧前缀、特性号常量、错误帧、电压→百分比插值 | 布局以 Solaar 源码为准，注释标行号；纯 bytes，无 I/O |
| `session.py` | 226 | `HidppSession`：对**单个**设备的协议编排（find_feature / read_battery / 消化通知帧） | transport-agnostic，USB 读写由注入的 transport 完成 |
| `state.py` | 178 | `BatteryPoller`：轮询节奏（正常 60s / 睡眠退避 5min）、变化检测、**多源仲裁** | session 当鸭子类型注入；纯标准库 |
| `lastknown.py` | 107 | **最后已知电量**持久化 `{设备名: (pct, ts)}` | 蓝牙 NULL 回落时兜底；I/O 失败静默降级；`BATBAR_LASTKNOWN` 可重定向 |
| `lowbattery.py` | 230 | 低电量提醒**状态机**：连续 3 次去抖 + 迟滞 + 充电/有线豁免 | `feed()` 纯判定可脱机测，副作用 `_deliver` 只在 agent 接线里调 |
| `winevents.py` | 257 | **事件去抖器 + WM_* 分类 + RAWINPUT 纯逻辑解码** | 纯逻辑零 ctypes（窗口/`SetTimer` 在 agent）；`due()` 带 `1e-9` 容差；`decode_rawinput` **按键位在 `ulButtons` 不在 `usFlags`** |
| `fullscreen.py` | 223 | **全屏/游戏感知**：QUNS 枚举分类 + 迟滞门 + `BATBAR_FULLSCREEN` 覆盖 | 纯逻辑零 ctypes（查询在 agent）；★ **`QUNS_BUSY` 也算"别打扰"**（无边框全屏实测返回它）；查询失败按"可打扰" |
| `migrate.py` | 158 | **历史数据复制搬迁**（`%LOCALAPPDATA%\logi-tray` → `\batbar` → 绿色模式的数据目录） | 只复制不删源；目标已有同名文件不覆盖；**最新来源优先**；不搬 `agent.pid`；失败一律静默；**必须早于 `applog.setup()`**（铁律 16） |

★ 这些模块**可在任意 Python 3.11+ 直接跑测试**（本项目用 3.13 实测）。

### 2.2 Windows-only 层（依赖 hidapi / 系统 API / 托盘）

| 文件 | 行数 | 唯一职责 | 关键约束 |
|---|---:|---|---|
| `transport.py` | 141 | **唯一** import `hid` 的地方；打开罗技 vendor collection | collection 选择对齐 Solaar base.py；import 名 `hid`（PyPI 包名 `hidapi`） |
| `agent.py` | 2673 | **主进程**：读设备 + 托盘 + 全部业务编排 | 见 §3 主循环与线程模型；改动前必读本文件顶部三张契约说明 |
| `bt_battery.py` | 645 | 蓝牙设备电量（一次 CIM 查询）+ **权威连接态过滤** | 判据演进史见 README §3.2；快判服务 `sync_now` 供手动刷新 |
| `dongle.py` | 463 | 2.4G 厂商通道（0xFF02）**被动监听**，拿实时电量与在离线 | **只读**，自己不写一字节；写查询由 poll24 用独立句柄做 |
| `poll24.py` | 251 | 2.4G **周期性主动查询**（官方 getBattery 帧），刷新静默期旧读数 | `SCAN_INTERVAL=60s`；`scan_now()` 供「立即刷新」 |
| `receiver24.py` | 340 | 三模键盘切 2.4G 后的**设备识别 + 在/离线/有线判据** | `wired_bases()` 默认非阻塞（后台线程） |
| `wired24.py` | 314 | **有线（USB 数据线）通道**：插线键盘识别 + 读电量 | 与接收器共用 VID/PID，靠产品串区分 |
| `feature24.py` | 248 | 0xFF04 口 Feature Report 采集 | ⚠️ **已证伪为静态配置 buffer**，非电量通道；只剩枚举+留证 |
| `win32ctypes.py` | 192 | **Win32 HID / SetupAPI 的 ctypes 共享声明（唯一真相源）** | ⚠️ 结构体与函数签名只此一份；**指针参数一律 `c_void_p`**；详见铁律 11 |
| `winproc.py` | 52 | **spawn 子进程的无窗口封装（唯一真相源）** | 一律用 `run_hidden()`；GUI 进程直接 `subprocess.run` 会弹控制台黑窗（铁律 21） |
| `icons.py` | 157 | Pillow 画**百分比数字** → PNG bytes | 32×32 渲染再降采样；不做描边、不画充电符号（用户定稿） |
| `singleinstance.py` | 215 | 命名互斥量，挡住第二个 agent | 防抢 HID 输入队列（详见该文件顶部「为什么必须有」） |
| `applog.py` | 120 | 日志落盘（落点由 `paths.py` 决定，滚动 1MB×3） | 幂等 setup；托盘无控制台，出问题只能靠文件 |
| `paths.py` | 187 | **落盘位置的唯一真相源**（绿色模式 / 不可写回退） | 唯一读 `%LOCALAPPDATA%` 的地方之一；缓存按输入签名（铁律 22） |

### 2.3 已删除模块（旧双进程架构，v0.1.4 移除）

| 文件 | 原行数 | 原职责 | 删除理由 |
|---|---:|---|---|
| `wire.py` | 103 | controller↔agent 桥接协议（行分隔 JSON） | 桥没了，协议无人用 |
| `net.py` | 43 | 网络定位辅助（查本机 IP 以连 agent） | 同上，且是唯一有联网动机的文件 |
| `controller.py` | 215 | 外部调度器 + 渲染 + TCP client | agent 不再监听 ⇒ 它永远连不上 |

> 这三个文件是「计算与渲染拆到另一进程 + 本地 TCP 桥」时期的产物。实测该拆分**没有收益**
> （Pillow 画 32×32 PNG 是微秒级，跨进程序列化反而多一层故障面），早已改回单进程自跑，
> 但文件一直留着「为了不破坏既有单测」。
>
> ⚠️ **留着的代价**：`agent.py` 里 `HOST = "0.0.0.0"` / `PORT = 51788` 与 `_serve()`
> 没跟着删 ⇒ 每次双击 exe 都开一个 `0.0.0.0:51788` 监听（**全部网卡**，不是 127.0.0.1），
> Windows 防火墙于是弹一次窗。2026-10-14 用户报的就是它。
>
> 结论：**死代码不是"无害的"，它是"没人再看的"** —— 主路径改了，它不会跟着改，也不会报错。

---

## 3. agent.py 内部结构（2668 行，最大文件，按需直达）

**为何这么大**：它是唯一的编排层——所有数据源、托盘、菜单、快照、低电量接线都汇到这里。
代价是它成了「改动热点」，所以单列一张内部路由。

### 3.1 线程模型

| 线程 | 启动处 | 职责 |
|---|---|---|
| **主线程** | `icon.run()` | Windows 托盘消息循环（系统强制要求），右键菜单 WndProc 在此 |
| 探测循环 | `_mouse_loop` 类 | 罗技鼠标在/离线探测（满足间隔 + 8s 读数保鲜期） |
| 蓝牙 warm loop | `_bt_warm_loop` | 周期刷新蓝牙全量（~9s 一轮），摘除/复活条目；**全屏态跳过** |
| 2.4G 扫描 loop | `poll24` 启动线程 | 每 60s 扫一遍；**全屏态跳过**（判据由 agent 注入，poll24 保持独立） |
| 全屏感知 loop | `_fs_poll_loop` | 每 2s 采一次 QUNS 维护迟滞门（只读、<0.1ms，故用 2s 细节拍） |

`_icon_lock`（RLock）串行化所有图标更新——并发更新会触发 **WinError 1402**（无效光标句柄）。

**消息线程（非轮询）**：`_start_theme_watch` 的隐藏窗口同线程还负责
`WM_DEVICECHANGE`（去抖后起守护线程跑刷新）、`WM_TIMER`（去抖到期）、
`WM_TASKBARCREATED`（重建托盘图标）、`WM_INPUT`（RAWINPUT 鼠标活动，去抖后
催一次罗技探测）。四条都是**事件驱动**，取代了原来「拔了线也要等下一轮轮询」
的延迟。窗口线程**不做重活**：收到事件只 `SetTimer` / 起线程，避免阻塞消息循环。

**四张 listener 注册表**（模块级，见铁律 12）：`_theme_listeners`（深浅色变化）、
`_device_listeners`（USB 插拔）、`_taskbar_listeners`（explorer 重启）、
`_mouse_listeners`（鼠标活动唤醒）。语义不同、互不混用；Agent 在 `__init__`
里各 append 一个方法。

### 3.2 图标刷新链路（用户最常感知的路径）

```
数据变化 → _apply_icon(info, force) → 内容变化？ → _set_icon(...)
                                    │              ├── _last_painted 缓存比对，未变则跳过
                                    │              └── shell 对同内容 NIM_MODIFY 不重绘
                                    └── 内容变化 → 重新注册（icon.visible=False/True）强制重绘
```

⚠️ **三个反直觉点**（都是血泪教训，改动前务必知道）：
1. **NIM_ADD 后的第一次 NIM_MODIFY 会被 shell 收下但不重绘**（返回 1、err=0 也不画）；
   内容变化必须走「重新注册」。
2. **`_wait_first_paint`** 让 NIM_ADD 帧自带真内容（不然启动瞬间是空白/`--`）；
   `_tray_shown` 门控保证展开菜单不会挡住 NIM_ADD（曾把注册拖到 +3.63s）。
3. 图标层**恒画 `--`**，永不画 `?`（用户硬需求）；percent=None 的幽灵条目不进快照。

### 3.3 菜单与快照

| 方法 | 作用 |
|---|---|
| `devices_snapshot()` | 汇总**所有数据源** → `{名称: 电量}`，菜单与图标都读它 |
| `_install_menu_refresh(icon)` | 右键弹出前重建菜单（pystray 默认复用旧 HMENU，会卡「启动中…」） |
| `_manual_refresh()` | 「立即刷新」：三路并行（鼠标直读 ∥ 蓝牙 sync_now ∥ 2.4G 扫描），**每步即时重绘** |
| `_repaint()` | `_apply_icon(None, False)` 全量重算选源（带锁，线程安全） |

---

## 4. 功能路由直达表

> 想改什么 → 直接跳到对应文件。避免「grep 半天才找到」。

| 目标 | 直达文件 | 备注 |
|---|---|---|
| **托盘图标外观/配色/字号** | `icons.py` | 用户定稿：单色数字、无描边、无充电符号 |
| **图标不刷新 / 重复注册 / 闪一下** | `agent.py` `_set_icon` / `_apply_icon` | 先看 §3.2 三个反直觉点 |
| **右键菜单项 / 菜单顺序** | `agent.py` `_menu()`、`_install_menu_refresh` | 菜单卡「启动中…」= 忘了重建 |
| **「立即刷新」做了哪些事** | `agent.py` `_manual_refresh` + `_bt_step` | 三路并行，各步即时 `_repaint` |
| **鼠标（罗技接收器）读不到电量** | `session.py` → `hidpp.py` | 帧布局以 Solaar 为准；`--probe` 先自检 |
| **某特性号/电量解析行为** | `hidpp.py` | 纯 bytes，改完直接跑 `test_hidpp.py` |
| **鼠标关机后条目不消失 / 误消失** | `agent.py` `_probe_mouse` / `_read_with_second_chance` | 判据 = **连续两次不应答 = 不在**（单次毛刺给复读机会） |
| **蓝牙设备电量读不到 / 兜底** | `bt_battery.py` + `lastknown.py` | 耳机 HFP 电量只刷新一次，之后 NULL |
| **蓝牙设备关机/上线不即时** | `bt_battery.py` `sync_now()` | 一次 WinRT 查询双向和解（实测 ~0.68s） |
| **蓝牙条目不消失（宽限期卡住）** | `bt_battery.py` `_accept` / `_all_offline_confirmed` | 空表 + WinRT 复核全离线 → 立即接受；不能删 180s 宽限 |
| **2.4G 键盘电量静默不更新** | `poll24.py`（主动查）+ `dongle.py`（被动听） | 事件驱动推送，静默期要靠轮询补 |
| **2.4G 键盘在/离线判定** | `receiver24.py` | 切换 dongle 后蓝牙节点仍在，须过滤 |
| **插数据线才出的键盘（关闭档）** | `wired24.py` | 无线全死，只有 USB 有线通道能读 |
| **改了扫描/轮询节奏** | `poll24.py` `SCAN_INTERVAL` / `state.py` 轮询参数 | `BATBAR_SCAN_24G` 可覆盖 |
| **低电量提醒频率/阈值/误报** | `lowbattery.py` | 去抖 3 次 + 迟滞 +5 + 充电豁免 |
| **托盘图标丢了（explorer 重启）** | `agent.py` `_on_taskbar_recreated` + `winevents.py` | ✅ 已监听 `WM_TASKBARCREATED`（动态注册号）；重发 NIM_ADD 后 ~272ms 回来 |
| **拔插 USB / 设备上了下了不刷新** | `agent.py` `_on_device_change` + `winevents.py` `EventDebouncer` | ✅ 已监听 `WM_DEVICECHANGE`；**必须去抖**（一次插拔 12 条消息） |
| **事件去抖节奏 / 消息号** | `winevents.py` `DEBOUNCE_SECONDS` / `classify()` | 600ms 静默期；`WM_TASKBARCREATED` 是动态号不能硬编码 |
| **鼠标开机后条目半天不复活** | `agent.py` `_on_mouse_activity` + `winevents.py` `decode_rawinput` | ✅ RAWINPUT 唤醒即刷新（2026-10-13）；去抖 250ms，`MOUSE_FRESH` 门控，**不清退避** |
| **RAWINPUT 收到但判成"无活动"（纯点击漏掉）** | `winevents.py` `decode_rawinput` | ★ 按键/滚轮位在 **`ulButtons`（偏移 28）不在 `usFlags`**；见该函数 docstring |
| **低电量提醒在游戏里突然冒出来** | `fullscreen.py` `FullscreenGate` + `agent._gate_notify` | ✅ 已接（2026-10-13）；门控在**投递前**才问，被抑制不补弹 |
| **游戏中还在周期读硬件 / 白烧 CPU** | `agent._in_dont_disturb` + `poll24.set_skip_when` | ✅ 已接；`_bt_warm_loop` 与 poll24 各自在轮首查门控 |
| **全屏感知没生效 / 想强制关掉** | `fullscreen.py` `ENV_MODE` | `BATBAR_FULLSCREEN=auto`(默认) \| `off`(永不静音) \| `on`(强制静音)；错值回落 auto |
| **全屏判定为什么认 `BUSY` 不认 `D3D_FULL_SCREEN`** | `fullscreen.is_dont_disturb` | ★ 实测无边框全屏（游戏最常见形态）返回 `QUNS_BUSY`；只认 D3D 会漏掉绝大多数游戏 |
| **设备枚举报 `TypeError: expected LP__XXX instead of pointer to _XXX`** | `win32ctypes.py` | ★ argtypes 被多模块互相覆盖 → 统一到共享模块；**签名一律加这里**（铁律 11） |
| **改任何 Win32 HID/SetupAPI 调用签名** | `win32ctypes.py` `ensure_argtypes()` | 业务模块里**绝不许**再写 `xxx.argtypes = [...]` |
| **启动链断了 / import 炸 / venv 缺依赖** | `tests/smoke_launch.py` | 真起 agent 读日志；单测查不出这类问题（§6-F） |
| **开机自启 / 常驻无控制台运行** | `scripts/register_autostart.ps1` + `scripts/agent-hidden.vbs` | 计划任务 + VBS 静默启动，无需管理员；任务名 `batbar-agent`。**exe 部署**则把计划任务的「操作」改指向 `dist\batbar.exe` |
| **打包成 exe / 改 exe 图标 / 版本信息** | `batbar.spec` + `build-exe.bat` + `assets/batbar.ico` + `scripts/version_info.txt` | 图标由 `tools/make_icon.py` 生成；改完重跑 `build-exe.bat`。spec 里的 `hiddenimports` 是**硬需求**（铁律 18） |
| **改 CI / 发新版本 / 自动出 Release** | `.github/workflows/build-exe.yml` + `.github/release-notes.md` + `.github/dist-extra/` | 推 tag 出 Release、推 `main` 出 artifact。**改 workflow 前先在本地按真实 `run` 块预演**（README §6-L）；脚本正文纯 ASCII，中文只进 YAML 注释。打包进 zip 的中文名文件放 `dist-extra/` 整目录复制 |
| **核验 CI/Release 产出的那份 exe** | `tools/verify_exe.py <exe 路径>` | 同一套验收标准套在「从 zip 解压出来的那份」上，不必改脚本 |
| **打包版跑 `--scan` 看不到输出 / 双击没反应** | `agent.attach_parent_console()` + `agent.install_crash_log()` | 双击没有控制台 ⇒ 请**从 cmd 里跑**；异常现在会落日志 `<数据目录>\logs\agent.log` |
| **`print` 在中文 cmd 上崩掉或乱码** | `agent.make_console_safe()` + `tests/test_menu_boot.py::ConsoleEncoding` | 只用 GBK 能编码的字符；单测会拦下（铁律 17） |
| **进程工作目录（cwd）/ 相对路径诡异行为** | `agent.anchor_cwd()` | 在 `main()` 里先于 argparse 调用；源码运行锚 `sys.base_prefix`，**打包后改锚 exe 所在目录**（`_MEIPASS` 绝不能锚，见铁律 18）；失败静默放弃（绝不让进程起不来） |
| **托盘程序运行时冒出黑色控制台窗口** | `winproc.run_hidden()` | spawn shell 一律走它；**禁止直接 `subprocess.run/Popen`**（铁律 21） |
| **改名/改位置后日志与电量表不见了** | `migrate.py` | 把历史位置（`logi-tray` → `batbar`）的数据**复制**进当前数据目录；只复制不删源、目标已有不覆盖、不搬 PID（README §6-J）；调用点须**晚于 `parse_args()`、早于 `applog.setup()`**（铁律 16，否则 `--help` 也会有落盘副作用） |
| **日志级别/落点/滚动** | `applog.py` | `BATBAR_DEBUG=1` 或 `--verbose` 开 DEBUG；落点见下一行 |
| **数据/日志/PID 到底写在哪儿** | `paths.py` + `tools/verify_green_mode.py` | 打包后 = **exe 同目录**（绿色模式）；exe 目录不可写自动回退 `%LOCALAPPDATA%\batbar`；**每次启动日志里有一行 `数据目录 = …`**。两个真机验收脚本：`verify_green_mode.py`（拷到干净目录跑，验数据确实落在旁边）与 `verify_green_fallback.py`（`icacls /deny` 造只读目录，验回退） |
| **日志路径（排障第一现场）** | `<数据目录>\logs\agent.log` | 打包后即 `batbar.exe` 旁边的 `logs\agent.log`（铁律 22） |
| **PID / 精准重启** | `<数据目录>\agent.pid` + `singleinstance.py` | 旧实例可能是 `python.exe`（非 pythonw），`taskkill /IM pythonw.exe` 杀不到 |

---

## 5. 改代码前必守的铁律

从踩过的坑里提炼，违反任一条大概率引入回归：

1. **纯逻辑与 I/O 严格分离**：`hidpp/session/state/lowbattery/lastknown/winevents/fullscreen/migrate`
   保持零第三方依赖 —— 这是「无需硬件即可跑 502 项测试」的前提，不要往里塞
   `import hid` / `import pystray` / subprocess。
2. **`transport.py` 是唯一能 `import hid` 的文件**。别的地方要碰 USB 一律走注入的 transport。
3. **`singleinstance.py` 不可绕过**：HID 每个 collection 只有一条输入队列，
   两个实例会**互相抢帧**（表现为「探针收不到任何帧」，容易被误判成「设备无实时通道」）。
4. **图标更新必须持 `_icon_lock`**：并发 `Shell_NotifyIcon` 会 WinError 1402。
5. **关机/离线判据 = 连续两次不应答**（不是「不应答 = 睡眠」——实测静置 40s 的 G304
   仍 0.20s 应答；接收器寄存器 0x02 在关机后也不掉，这两个旧判据都被证伪删除）。
6. **蓝牙空表宽限（`EMPTY_GRACE=180s`）是防 CIM 抖动的，不能见空就清**；
   要提前清必须走 `_all_offline_confirmed()`（WinRT 权威复核）。
7. **Windows 侧脚本正文只写 ASCII**（无 BOM 的 UTF-8 `.ps1` 在 PS 5.1 下按 GBK 读，
   中文会乱码）；`>>` 重定向是 UTF-16LE，写环境变量类文件要用
   `[IO.File]::AppendAllText` + `UTF8Encoding($false)`。
8. **改完必跑**：`.venv/Scripts/python.exe -m pytest tests/ -q`（当前 502 项应全绿），
   再 `py_compile` 改动文件。
9. **改动前先读 `README.md` 对应章节**：很多「看似该这么改」的地方，
   README 里记着「为什么不能这么改」（如 0x02 寄存器、feature24 语义）。
10. **Win32 广播事件必须去抖**：`WM_DEVICECHANGE` 一次插拔实测 12 条，
    逐条处理会打爆 HID 队列；去抖要用 `SetTimer`（消息队列级），
    **不要用线程 `sleep`**（占线程 + 与消息循环竞态）。窗口过程里只做
    轻量活（起线程 / 置计时器），重活（读硬件）一律甩到守护线程。
11. **★ Win32 ctypes 签名只准在 `win32ctypes.py` 写一次**：`ctypes.windll.setupapi`
    / `hid` 是**进程级单例**，`argtypes` 是**全局可变状态**，多模块各设一遍
    **最后设置者赢**。之后别的模块拿自己那份结构体（同名不同类对象）去调，
    ctypes 按**类型身份**校验，直接抛
    `TypeError: expected LP__GUID/_IFD/_CAPS/_ATTRS instance instead of pointer to _…`。
    **异常被上层吞掉后不崩溃**，只表现为「2.4G 条目悄悄从菜单消失」。
    ⇒ 结构体（`GUID/IFD/DETAIL/ATTRS/CAPS`）与签名**只此一份**；指针参数
    一律 `c_void_p`（`byref(任意 Structure)` 合法）；`ensure_argtypes()` 幂等。
    新增 Win32 调用时签名加进 `win32ctypes.py`，**业务模块里绝不许写
    `xxx.argtypes = [...]`**。复核脚本：`probes/verify_argtypes_shared.py`。
    **单测查不出这个 bug**（mush 全绿时它仍在）。
12. **新事件源一律走「注册表 + 回调」，不改窗口过程去 `import Agent`**：
    `_theme_listeners` / `_device_listeners` / `_taskbar_listeners` / `_mouse_listeners`
    四张注册表**互相独立**（语义不同不能混）；Agent 在 `__init__` 里 append 自己的方法，
    窗口过程只 `for cb in list(_xxx_listeners): cb()`（带 try/except 隔离，一个订阅者
    失败不拖累其余）。回调里**只做轻量活**（起线程/置定时器），重活甩守护线程。
13. **`_enable_dark_menus()` 必须在 `applog.setup()` 之后、`Agent()` 之前**：
    它里面会起 theme-watch 窗口并注册 RAWINPUT，日志走 INFO/DEBUG —— 若在日志装好
    之前调用，「RAWINPUT 鼠标监听已注册」这类关键证据会**静默丢失**，排障只能盲猜。
    （2026-10-13 实际踩到：改完 RAWINPUT 后日志里查不到注册行，一度以为没生效。）
14. **跨模块的"环境感知"一律用「注入判据」，不许反向 import**：
    `poll24` 是独立模块，**不能**为了跳过扫描去 `import agent`（会成环、也破坏
    模块自治）。做法 = 模块自己留一个 `set_skip_when(pred)` 注入口，agent 启动时
    把 `self._in_dont_disturb` 注进去。同理 `lowbattery` 用的是构造参数
    `gate=`（与既有 `wired_check=` 同款模式）。**注入的判据自己抛异常时，
    一律按"不生效"处理**（宁可多干一轮活，也别永远不干）。
15. **启动入口必须锚定 cwd，且锚定动作只能「尽力而为」**：agent 会被注册表
    `Run` 键 / 计划任务 / `restart-diag.bat` 拉起，cwd 常见是 `System32`。
    当前**所有落盘路径都由 `paths.py` 统一推导**（见 §2 的 `applog` /
    `lastknown` / `singleinstance`；打包后跟 exe 同目录，见铁律 22），
    「本机测试永远正常、只在用户开机自启时炸」的幽灵故障。⇒ `main()` 里先于
    argparse 调 `anchor_cwd()`。锚点用 `sys.base_prefix`（**不是** `sys.prefix`：
    venv 里它指向 `.venv`）；目标不存在或 `chdir` 抛异常**一律静默放弃** ——
    锚定失败不是致命错误，绝不能因此让进程起不来。
16. **数据搬迁必须早于 `applog.setup()`，晚于 `parse_args()`，且只复制不移动**：
    `migrate.py` 搬的东西之一就是旧日志文件本身（`%LOCALAPPDATA%\logi-tray` →
    当前数据目录）。若先装好 handler 再搬，handler 已抓住旧路径的文件句柄 ⇒ 搬不动
    （Windows 上表现为「文件被占用」），且新日志仍写旧目录，搬迁**看起来生效了其实
    没有**。 ⇒ 顺序固定为
    `anchor_cwd()` → `parse_args()` → `migrate.migrate()` → `applog.setup()`。
    ★ **为什么必须晚于 `parse_args()`**：`--help` / 参数敲错时 argparse 直接退出，
    这种**一次性命令不许有任何落盘副作用**。不是洁癖 —— 绿色模式下数据目录就是
    exe 所在目录，而 CI 的冒烟步骤正是把 exe 放进发布目录后真跑一次 `--help`；
    搬迁一跑就把 `%LOCALAPPDATA%` 的历史存档复制进发布目录，随后被打进 zip 随
    Release 发出去（2026-10-14 v0.1.2 首次构建实际发生，附件里多了一个
    `last-known.json`）。守这条的有两道：`tests/test_migrate.py` 的源码级顺序断言，
    与 CI 冒烟步骤的「发布目录快照前后必须一模一样」比对。
    语义（2026-10-13 用户确认改成绿色模式后）：**复制，不移动、不删源**（旧目录留着
    可回退）；**目标已有同名文件就跳过，绝不覆盖**；**越新的来源越先处理**
    （`reversed(legacy_dirs())` —— 顺序写反会让老数据顶掉新数据）；**不搬
    `agent.pid`**（旧 PID 若被复用，重启脚本会误杀无辜进程）。
    失败一律静默 —— 搬迁是善后，不是断言。
17. **`print()` 只准用 GBK 能编码的字符**（2026-10-13 打包时踩到）：中文 Windows
    的 `sys.stdout.encoding` 是 **cp936**，写 emoji 会抛 `UnicodeEncodeError` 并
    **直接终止进程**（不是"打不出来"那么轻）。`--scan` 就栽在「✅ 可用写长度」
    那一行：rc=1、**日志一片空白**（print 不走 logging，完全没有留痕）。
    最坑的是 Git Bash / VS Code 的 UTF-8 终端**永远复现不出来**。
    ⇒ ① 字符本身换成 `√`/`×`；② `main()` 里 `make_console_safe()` 把
    stdout/stderr 的 errors 调成 `replace` 兜底；③ `test_menu_boot.py::ConsoleEncoding`
    扫 `src/` 所有 `print()` 行守住这条 —— **改 print 之前先跑它**。
18. **打包（frozen）的三处特判，位置各不相同**（2026-10-13）：
    ① `anchor_cwd()` 在 `sys.frozen` 下改锚 `os.path.dirname(sys.executable)` ——
    这时 `sys.base_prefix` 是 `_MEIPASS`（onefile 为 `%TEMP%\_MEIxxx`），锚它会
    让 Windows 拒绝删除「正被当作 cwd 的目录」，顶掉 bootloader 的临时目录清理；
    ② `attach_parent_console()` 把 windowed exe 的输出接到调用者控制台 ——
    `sys.stdout is None` 时 CPython 的 `print` 是**静默 no-op**，比报错更难查；
    ③ `install_crash_log()` 装 excepthook 把 traceback 写日志（windowed 无 stderr，
    异常凭空消失）。
    ⚠️ **顺序不是一个**：`attach_parent_console()` / `make_console_safe()` 必须在
    `applog.setup()` **之前**（setup 靠「stdout 是不是 None」决定装不装控制台
    handler），`install_crash_log()` 必须在 `applog.setup()` **之后**（早装则日志
    还没有文件 handler）。
19. **改 `.github/workflows/*.yml` 时，`run` 块正文一律纯 ASCII**（2026-10-13）：
    Windows PowerShell 对**无 BOM 的 UTF-8 脚本**按本地代码页（GBK）解码，正文
    里的中文被误解后可能产生引号类字节而打断解析（实测报「字符串缺少终止符」
    「，后面缺少表达式」）。中文写 YAML 注释（与 `run:` 同缩进、在其上方）。
    ★ 要打进 zip 的**中文文件名**也不能出现在正文里 —— 放 `.github/dist-extra/`
    整目录复制，脚本里只写 `*.txt` 这类通配。
    ⇒ 同一条纪律在 `print()` 上也有（铁律 17），根因都是 cp936。
20. **推之前先在本地按真实 `run` 块预演 CI**（2026-10-13）：别盲推 workflow 然后
    反复看日志 —— 那是最慢的路径。用 `github-actions-win-release` 技能的
    `run_workflow_steps.py` 把 YAML 里的 `run` 块抽出来真跑一遍。当轮预演当场
    查出三处只在 CI 才炸的问题（run 块中文注释 / `$env:RUNNER_TEMP` 为空 /
    onefile 拆解异步导致误判进程残留），一处都没留给 CI 去发现。
    ⚠️ 该驱动的输出目录里也有它自己，而它执行目录下**所有** `*.ps1` ⇒ 会递归调用
    自己（嵌套运行的 `Set-Content` 反复截断同一份日志，现象是「日志只剩第一步」）。
    把驱动复制到目录外**并删掉原件**才干净。
21. **★ GUI 进程里 spawn 子进程一律走 `winproc.run_hidden()`**（2026-10-13 用户报告）：
    打包后是 `console=False` 的 GUI 子系统程序、**没有控制台**，spawn console 程序
    （`pwsh` / `powershell`）会被 Windows 自动分配一个新控制台 ⇒ 屏幕上冒黑窗
    （启动时 + 每次刷新动作都有，用户截图报的就是这个）。
    实测对照（pythonw 无控制台进程跑；三种模式都确认子进程**真起来了**，排除
    "没调所以没窗口"的假象）：不带 flag 新建 `ConsoleWindowClass`，标题
    `管理员:C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.6.0_...\pwsh.exe`
    —— 即本机 `shutil.which("pwsh")` 指向的 MSIX 别名；带
    `CREATE_NO_WINDOW` + `STARTUPINFO(SW_HIDE)` → 新窗口 **0**。
    ⇒ 唯一入口 `winproc.run_hidden()`，**业务模块禁止直接调 `subprocess.run/Popen`**，
    `tests/test_no_console_flash.py` 扫源码守门（`winproc.py` 是唯一例外）。
    与铁律 17 同源：**只在 GUI / 无控制台场景暴露**，源码运行永远测不出。
22. **★ 落盘位置只有一个真相源 `paths.py`，且"不可写"必须有回退**（2026-10-13
    用户要求改成绿色模式）：打包后数据（`logs\` / `last-known.json` / `agent.pid`）
    **平铺在 `batbar.exe` 同目录**，整个文件夹拷走即带走全部数据。
    两条硬约束：
    ① **别处不许自己拼路径** —— 之前 `applog` / `lastknown` / `singleinstance` /
       `migrate` 四家各拼一遍 `%LOCALAPPDATA%\batbar`，改一处忘一处迟早出事。
       `tests/test_paths.py` 扫源码守门：除 `paths.py` / `migrate.py` 外，任何模块
       读 `LOCALAPPDATA` 就算回归。
    ② **exe 目录不可写必须回退到 `%LOCALAPPDATA%\batbar`** —— 用户完全可能把 exe
       解到 `Program Files` 或只读盘；**绝不能因为写不了就不落盘**（没日志的托盘
       程序等于瞎跑）。探测方式是**真建目录 + 写探针文件再删**，不用
       `os.access(W_OK)`（Windows 上它对 ACL / UAC 虚拟化判断不可靠）。
       每次启动日志打一行 `数据目录 = …`，直接回答"数据在哪"。
    ⚠️ **缓存要按输入签名**（`BATBAR_DATA_DIR` / `LOCALAPPDATA` / `APPDATA` /
    frozen / exe 路径），不能裸存一个值 —— 环境变量变了却返回旧路径是最阴的一类
    bug，单测里真踩过（一个用例清空 `LOCALAPPDATA` 调了一次，把"用户目录"落点
    缓存住，污染了后面另一个用例的断言）。
    源码运行仍走 `%LOCALAPPDATA%\batbar`：绿色模式只针对分发出去的 exe，
    否则开发时会在仓库根目录里拉屎。

23. **★ 托盘 exe 不许开监听端口**（2026-10-14 用户报防火墙弹窗）：batbar 读电量
    走**本机 HID**，全程不联网 ⇒ **任何 `.bind(` / `listen(` 都不该存在**。
    由来：早期是「Windows 侧 agent + WSL2 侧 controller」双进程，靠本地 TCP
    （`0.0.0.0:51788`）通信；改单进程自跑后 controller 废弃，监听却留在
    `agent.py` 里没删 ⇒ 每换一台机器首次启动，防火墙都要问一次。
    ⚠️ **两条教训**：
    ① **删功能要删干净** —— 那段死代码是"双进程时代的化石"，`_serve()` / `_broadcast()`
       / `_clients` 三个连带产物一起删，还顺手摘掉了 `import socket`/`struct` 里多余的那个。
    ② **"没有症状"不等于"没问题"** —— 防火墙提示**每台机器通常只弹一次**，
       本机点过「取消」就再也复现不出来 ⇒ 判据必须是客观可复现的
       （`tools/verify_no_listener.py`：真起 exe → 枚举该 PID 的 TCP 监听 → 必须为空，
       且**自带阳性对照**证明枚举方法有效）。
    判据口径：不看"有没有 `import socket`"，只看"有没有真的 listen"。
    守卫在 `tests/test_no_listener.py`：禁 `.bind(` / `listen(`、禁 agent 用 socket、
    禁 `HOST`/`PORT` 常量、并断言 `wire/net/controller` 三个文件**不存在**（防复活）。

---

## 6. 已知待办 / 可借鉴的改进方向

> 与同类项目 FerrisPulse 对比后识别出的增强点。

> ✅ **2026-10-13：本表已全部清空。** 下面保留已完成项与其中的「边界纪律」，
> 作为历史记录 —— 新增待办请往上加一行。

| 方向 | 现状 | 可借鉴做法 | 收益 |
|---|---|---|---|
| ~~设备插拔事件驱动~~ | ✅ **已完成**（2026-10-13，`winevents.py`） | 监听 `WM_DEVICECHANGE`（去抖 600ms） | 拔插从秒级降到「消息到达即刷新」，真机四组操作全中 |
| ~~托盘自愈~~ | ✅ **已完成**（2026-10-13） | 监听 `WM_TASKBARCREATED` → 重发 NIM_ADD | explorer 重启后 ~272ms 图标回来 |
| ~~黑盒冒烟测试~~ | ✅ **已完成**（2026-10-13，`tests/smoke_launch.py`） | 真 spawn agent 子进程 + 读真实日志验启动链（`boot:` 打点） | 抓到 mock 测不出的真 bug：相对导入 + ctypes argtypes 覆盖；`smoke-test.bat` 双击即跑 |
| ~~鼠标硬件唤醒检测~~ | ✅ **已完成**（2026-10-13，`winevents.py` + `agent.py`） | `RAWINPUT` + `RIDEV_INPUTSINK`（后台无焦点也收）→ **唤醒即刷新**：鼠标一有活动就催一次 HID++ 探测（去抖 250ms，`MOUSE_FRESH` 门控，不碰退避） | 鼠标开机后条目**秒回**，不再等 9s 轮询（退避时最多省 30s）。真机实测 971 条输入 100% 解码正确、去抖成 11 次事件 |
| ~~全屏/游戏感知~~ | ✅ **已完成**（2026-10-13，`fullscreen.py` + `agent.py`） | `SHQueryUserNotificationState`（QUNS）采样 → 迟滞门 → **静音提醒 + 暂停后台轮询**；`BATBAR_FULLSCREEN=auto\|off\|on` 可覆盖 | 全屏/游戏中不弹 toast、不周期读硬件；真机实测造全屏窗口 6s 进入静音、关窗 10s 恢复。★ 实测无边框全屏返回 `QUNS_BUSY` 而非 `D3D_FULL_SCREEN` |
| ~~启动锚定工作目录~~ | ✅ **已完成**（2026-10-13，`agent.anchor_cwd()`） | `os.chdir(sys.base_prefix)` 防 cwd 漂移，在 `main()` 里**先于 argparse** 调用 | 开机自启/计划任务/`restart-diag.bat` 的 cwd 不再停在 System32；真机实测 `System32 → ...\cpython-3.12-...`。**当前代码零相对路径，属防御性保险**（挡「将来有人写相对路径」那类只在用户机上炸的 bug） |
| ~~项目改名 + 数据目录迁移~~ | ✅ **已完成**（2026-10-13，`logi-tray` → `batbar`，`migrate.py`） | 进程级常量全量替换（包名/logger/`BATBAR_*`/互斥量/窗口类/线程名/临时前缀/计划任务/entry point）+ 旧 `%LOCALAPPDATA%` 数据一次性搬迁 | 名字不再绑罗技品牌（现覆盖鼠标/耳机/机械键盘三类）；旧日志与电量表无缝续用。⚠️ venv 的 `.pth`/`dist-info`/console script 带旧绝对路径，**改名后必须重装可编辑包**（README §6-J.4） |
| ~~打包成单文件 exe~~ | ✅ **已完成**（2026-10-13，`batbar.spec` + `build-exe.bat`） | PyInstaller onefile + `console=False`；`hiddenimports` 点名 `pystray._win32` / `hid`，`excludes` 掉 `tkinter`（Pillow 的 hook 会拉 tcl/tk ~12 MB） | **17,067,889 B ≈ 16.28 MB**，双击即用、目标机零 Python 依赖；托盘首帧 **+0.99s**。⚠️ 顺带挖出「cp936 下 `print` emoji 直接杀进程」这个真 bug（README §8 坑 20 / 铁律 17） |
| ~~CI 自动构建 + Release~~ | ✅ **已完成**（2026-10-13，`.github/workflows/build-exe.yml`） | `windows-latest` 上：setup-python → 装依赖 → **pytest 当闸门** → PyInstaller → 组装 `package/` → 冒烟（含真跑 `--help`）→ 手写 zip → 核对 → artifact；tag 触发则 `gh release create` | 白送了「干净 runner 上跑 502 项测试」这道闸门 —— batbar 的测试不需要 HID 硬件，所以 CI 能真跑而不是走个形式。发布目录刻意叫 `package/`（`dist/` 是 PyInstaller 的输出，重名会删掉刚编出来的 exe） |

> ⚠️ **RAWINPUT 三条边界的纪律**（写在 `winevents.py` RAWINPUT 节）：① 拿不到「是哪只鼠标」，只能当**催化剂**、绝不当在线判据；② 不是罗技私有信息（HID++ 才有 frame）；③ 频率极高（实测 ~33 条/s）**必须去抖**。

**明确不学 FerrisPulse 的地方**：它**没有日志系统**（全程 `catch { }` 静默吞异常）。
本项目的 `applog.py` 分级日志 + `probes/` 取证探针是核心优势——排障能力全靠它，守住。

---

## 7. 参考

| 文档 | 内容 |
|---|---|
| `README.md` | 选型理由、运行步骤、协议逆向全过程（④ 系列）、各轮 bug 修复记录 |
| `ARCHITECTURE.md`（本文件） | 模块职责、功能路由、改代码铁律、待办 |
| `tests/` | 502 项单测，全 stdlib；每个测试文件对应一个模块的契约。**外加 `smoke_launch.py` 黑盒冒烟**（真 spawn agent 验启动链） |
| `probes/` | 一次性取证探针（`probe_*.py` / `exp_*.py` / `verify_*.py`）+ 真实数据样本；查协议/时序/ctypes 签名时的现场证据。**已随仓库开源**，索引见 `probes/README.md` |
| `tools/` | 常驻工具（keybeat 节拍助手、probe_query、feature_watch、verify_green_mode / verify_green_fallback / verify_no_listener） |

---

## 8. 已删除的架构（考古区）

> 这里记录**已经不存在**的东西。留着不是为了怀旧，是为了防止有人"顺手抄回旧代码"——
> 每一条都写清了它为什么死，以及它死前惹过什么祸。

### 8.1 双进程 + 本地 TCP 桥（agent ↔ controller）

**形态**：Windows 侧 `agent.py` 开 `0.0.0.0:51788` 监听；WSL2 侧 `controller.py` 连过来，
两者用行分隔 JSON（`wire.py` 编码）通信；`net.py` 负责在 Windows 上找本机 IP 给 controller 连。

**为什么死**：拆进程没有收益 —— Pillow 画一张 32×32 PNG 是微秒级，跨进程序列化反而多一层
故障面。改回单进程自跑后，controller 那条路就断了。

**死前惹的祸**：`agent.py` 里的 `HOST` / `PORT` / `_serve()` / `_broadcast()` / `_clients`
跟着废弃但没删 ⇒ 每次启动都开一个对全部网卡的监听 ⇒ **Windows 防火墙弹窗**
（2026-10-14 用户报，v0.1.3 删监听、v0.1.4 删模块）。

**不要做的事**：
- 别再给托盘程序加任何形式的监听（HTTP 健康检查、WebSocket 调试口…）——
  一个常驻托盘程序需要的"外部控制"，用托盘菜单就够了。
- 别为了"检测网络"引入 socket：batbar 读电量走本机 HID，全程不联网。
- 恢复这三个文件的正确姿势是**从 git 历史里 `git show` 出来看**，不是 `git checkout` 回来用。
