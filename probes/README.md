# probes/ —— 取证探针：一手的现场证据

这里放的是开发过程中**一次性写就的取证脚本**，用来回答同一个问题：

> **「真机上到底发生了什么？」**

它们不是产品代码、不进单测、不保证接口稳定。但本项目的几乎每一个结论
（`README.md` 的「协议逆向全过程」、`ARCHITECTURE.md` 的坑与铁律、各轮 bug 修复）
背后都有这里的一个脚本作为证据 —— **没有实测支撑的推断，一律不算结论**。

## 为什么要把它们开源

`README.md` 里有大量「实测」「真机验证」的叙述。只放结论、不放过程，
读者就只能选择信或不信。把探针一并放出来，是让这些结论**可复核**：
你可以自己跑一遍，看到同样的原始返回值。

> ⚠️ **它们是原样保留的**，没有做工程化打磨：多为一次性脚本，含针对当时那台
> 机器的槽位/设备名假设，输出就是直接 `print`。**读它们是为了看清「怎么测的」，
> 而不是当工具库调用。** 每个文件头部 docstring 都写了它要回答的问题和用法。

## 怎么跑

在**项目根目录**执行（脚本会自己把 `../src` 注入 `sys.path`）：

```bat
.venv\Scripts\python.exe probes\probe_bt_all_status.py
```

前置：`Python 3.11+` 且已装依赖（`uv sync --extra agent` 或 `pip install -e ".[agent]"`）。

| 说明 | |
|---|---|
| 需要真实设备在位 | 读电量的探针（`probe_bt_*`、`verify_mouse_revive.py`、`exp_*`） |
| 无需设备也能跑 | 纯 Windows API 类的（`probe_quns.py`、`probe_rawinput*.py`、`verify_cwd_anchor.py`、`verify_argtypes_shared.py`） |
| **建议先退出托盘 agent** | 抢接收器输入队列的探针（`probe_sleep_deep.py` 头部明确要求） |

---

## 目录

### 1. 蓝牙：离线判据到底谁说了算

想解决的问题：设备真关机时，怎么才能**准确且不陈旧**地报「离线」。

| 探针 | 回答的问题 |
|---|---|
| `probe_bt_all_status.py` | 枚举所有配对设备，对每台**四源并排**（`pnp_present` / `hid_active` / `classic` / `le`）——谁在真关机时报得准。用法：设备**开着**跑一次做基线，**关掉**再跑一次做对照 |
| `probe_bt_last_device.py` | 最后一台蓝牙设备关闭后，底层查询链各环节分别返回什么（整表空 → 触发了宽限期逻辑） |
| `probe_bt_now.py` | 此刻蓝牙电量链路原始返回什么，不碰 HID、不影响托盘进程 |
| `probe_sleep_deep.py` | 让鼠标静置约 40s 入睡后，分别用「短枪 / 长枪 / 寄存器 0x02」探：① 睡眠中能否被叫醒 ② 寄存器 0x02 报的到底是不是电量 |
| `probe_sync_now.py` | `sync_now` 双向快判的耗时与准确性：把在连设备「藏起来」再同步，看能否靠 WinRT 秒判上线 |

### 2. 启动与托盘：慢在哪一步

想解决的问题：用户报「只有一个鼠标时，要 6~7 秒才显示电量」。

| 探针 | 回答的问题 |
|---|---|
| `probe_boot_cost.py` | 启动首轮各阶段耗时分解（蓝牙 CIM 查询 / HID 枚举 / 建会话含槽位扫描 / 单槽探测预算） |
| `probe_boot_timing.py` | 逐 import 打点，找出 import 成本的大头（不建托盘、不碰硬件） |
| `relaunch_agent.py` | 分离启动一个 `--standalone` agent，随后把日志里的 boot 打点打出来 |
| `probe_tray_modify.py` | **直接抓 `Shell_NotifyIcon` 的返回值 + `GetLastError`**，判定「首次 `NIM_MODIFY` 是被 shell 拒收了，还是收下了但没重绘」——两种原因的修法完全不同 |

### 3. HID++ / 厂商私有协议

想解决的问题：罗技 HID++ 的帧长、应答格式，以及机械键盘（迈从/LEOBOG）的私有 2.4G 通道怎么读电量。

| 探针 | 回答的问题 |
|---|---|
| `verify_mouse_revive.py` | 接收器插着但罗技条目消失时，「唤醒预算 + 死会话重扫」能否把鼠标救回来 |
| `exp_frame_len.py` | 帧长假说验证：`0xFF02` 的 `OutputReportByteLength` 是 20 还是 21 |
| `exp_battery_ack.py` | 捕捉迈从 `getBattery` 命令的**应答帧**，核对官方逆向出的解析流程（拼多包 → `batteryLevel`） |
| `exp_replay_1604.py` | 逐动作复现 16:04 那一轮操作序列，定位 backlight 通知帧的真实触发者 |
| `exp_setfeat_notify.py` | `SetFeature(06 4a ...)` 会不会触发 `0xFF02` 的 input 通知帧（延迟响应） |

### 4. Windows API / 消息取证

想解决的问题：Win32 层面的边界条件 —— 结构体尺寸、全屏判定、事件收不到、ctypes 全局状态。

| 探针 | 回答的问题 |
|---|---|
| `probe_device_events.py` | 探针 1：隐藏窗口为什么收不到 `WM_DEVICECHANGE` / `WM_TASKBARCREATED` |
| `probe_device_events2.py` | 探针 2：用几种不同窗口样式对照 + `CreateWindowExW` 真顶层窗 + `RegisterDeviceNotification` 双管齐下，区分是「窗口样式问题」还是「事件确实不广播」 |
| `probe_rawinput.py` | 隐藏窗口 + `RIDEV_INPUTSINK` 能否收到 `WM_INPUT`（关键是**后台无焦点**窗口），以及能否区分鼠标与其它 HID |
| `probe_rawinput_size.py` | 把 `GetRawInputData` 回调里每一步的尺寸都打出来，定位 `Buffer size too small (24 instead of at least 32)` 到底是哪个参数写错（`RAWINPUT` 尺寸语义） |
| `probe_quns.py` | 先确认 `SHQueryUserNotificationState` 能否调到、非全屏时返回什么、各枚举值语义 |
| `probe_quns_fullscreen.py` | 真造一个覆盖全屏的 `WS_POPUP` 窗口，看 `SHQueryUserNotificationState` 到底返回哪个枚举值（★ 实测返回 `QUNS_BUSY` 而非 `D3D_FULL_SCREEN`） |
| `verify_argtypes_shared.py` | 铁证：证明四个模块共用同一批 `ctypes` `argtypes`（直接读 `windll` 上的对象，验元素是否都是 `c_void_p`） |
| `verify_cwd_anchor.py` | 子进程 cwd 被设成 `C:\Windows\System32` 时，`anchor_cwd()` 是否真把工作目录搬走 |

### 5. 真实数据样本

原始抓取记录，供对照和复盘（不是脚本）。

| 文件 | 内容 |
|---|---|
| `snap-listen2.log` | 2.4G 厂商通道一次 600s 监听窗口的原始抓包（29 帧，含 `battery` / `backlight` / `handshake` 解析）—— `tests/test_poll24.py` 的样本来源 |
| `feature-samples-20261012.md` | 两台机械键盘 `0xFF04` 口的 Feature Report 周期采样（60s 间隔 × 1 小时），含 `06 00 02 64 64 64 xx xx` 原始字节 |
| `keybeat-20261012-031930.md` | 按键 → 电量帧时序报告（方案 quick / 120 秒） |
| `keybeat-20261012-032409.md` | 同上（方案 wake / 95 秒） |
| `keybeat-20261012-032702.md` | 同上（方案 wake / 95 秒，第 2 次） |

> 生成工具见 `tools/keybeat.py` 与 `tools/feature_watch.py`。

---

## 怎么读这些脚本

它们体现的是**同一套取证习惯**，比单个结论更值钱：

1. **一次只问一个问题** —— 脚本名就是那个问题。
2. **先立假设，再找能证伪它的观测**。`probe_sleep_deep.py` 存在的唯一目的就是推翻
   「不应答 = 睡眠」这个想当然的结论。
3. **对照实验** —— 同一探针在两种状态下各跑一次（`probe_bt_all_status.py` 的
   docstring 明确写了「设备开着跑一次、关掉再跑一次」）。
4. **读系统原始返回值，不读自己的缓存**。`probe_tray_modify.py` 抓的是
   `Shell_NotifyIcon` 的返回码和 `GetLastError`，而不是「图标看起来变了没有」。
5. **结论和时间戳一起落盘**（`keybeat-*.md`、`feature-samples-*.md`、`snap-listen2.log`），
   事后能复盘「当时到底看到了什么」。

## 与 `tools/` 的区别

| | `probes/`（本目录） | `tools/` |
|---|---|---|
| 性质 | 一次性取证，问完即弃 | 常驻工具，反复使用 |
| 典型 | 「这次到底返回了啥？」 | `keybeat.py`（节拍助手）、`probe_query.py`（批量查询）、`feature_watch.py`（长时采样） |
| 进单测吗 | 否 | 否（但被 README 的运行步骤直接引用） |

## 关于源码注释里的 `_tmp/`

`src/`、`tests/` 的注释里若出现 `_tmp/`，指的是**开发期的本地临时目录**（`.gitignore`
已排除，不随仓库分发）。其中被反复引用的探针**都已收录到本目录**，所以你现在在源码里
看到的基本是 `probes/xxx.py`。

仍然保留 `_tmp` 字样的只剩两类，都是**未分发的原始素材或运行时行为**：

- `poll24.py` 提到的罗技官方 Hub bundle `hub_protocol.js`（第三方产物，不随仓库分发）；
- `tools/` 各工具的 `--out` **默认输出目录**（运行时行为）。例如 `feature_watch.py` 默认把
  样本落到 `_tmp/feature-samples-<日期>.md` —— 本目录里的那两份样本就是从那儿挑出来的。
