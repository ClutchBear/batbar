## 下载

**`batbar-*-win64.zip`** —— 解压即用，**不需要装 Python**，不需要任何运行时。

1. 解压到任意位置（例如 `D:\batbar\`）
2. 双击 `batbar.exe` —— 它会常驻系统托盘（**没有窗口，属正常**）
3. 退出：托盘图标右键 → 退出

压缩包里还附了 `使用说明.txt`，含配置项与故障排查。

## 本版变化（v0.1.6）

**一次性能修复 + 一处菜单精简。** 右键菜单的弹出会明显变快，日志里也不再出现
重复的诊断行。

### 1. 右键菜单不再把硬件探测跑两遍（本版核心）

📌 **这个坑的排查过程值得记一笔**：日志里每次右键都出现**连续两行**
`菜单快照：{...}`。第一反应是「自己装的菜单刷新钩子被注册了两遍」——
**查证后否掉了这个假设**：钩子带幂等标记，且 pystray 的窗口过程按
`_message_handlers` 字典查表，一条消息只会命中一个处理器。

**真因在 pystray 里**：

- `Menu.items` 是个 **property**。传 callable 进去时它的实现是
  `return self._items[0]()` —— **每访问一次就重跑一遍工厂**
- 而 `pystray._win32._create_menu()` 建一次菜单要访问它**两回**：
  `if not descriptors` 走 `__bool__`，`for ... in enumerate(descriptors)` 走
  `__iter__`，两条路都汇到 `_visible_items() → items`
- 实测：一次构建，工厂正好被调用 **2 次**

所以每次右键弹出，菜单工厂被跑了两次 —— 而工厂里做的事是
`devices_snapshot()`（PowerShell CIM 查询 + USB 枚举，实测 ~3s），
整个过程**阻塞托盘线程**。

| | 修复前 | 修复后 |
|---|---|---|
| 右键弹菜单的开销 | hardware poll × 2 | hardware poll × 1 |
| `菜单快照` 日志行 | 每次右键 2 行 | 每次右键恰好 1 行 |

**修法**：钩子在弹出前自己求值一次，交给 pystray 的是一个**具体的 tuple**
（`Menu(*items)`）—— 对非 callable，`Menu.items` 直接返回 `_items`，再也不重跑。
初始菜单同样改成 `Menu(*_static_items())`，免得启动那次
`_mark_ready → update_menu()` 也白跑两遍。

### 2. 菜单去掉「打开日志」

日志就落在 `batbar.exe` 旁边（绿色模式，见 v0.1.2），直接就能看到，
托盘菜单里那一项多余。连带删掉了只为它服务的 `applog.current_path()`
和对应单测。**功能与用法不受影响。**

### 3. 测试与守卫

507 项单测全绿（+5），新增里有一条**阳性对照**：用真 `pystray.Menu`
钉住上游行为 —— 哪天 pystray 改成只访问一次，这条会红，
提醒我们「自己求值」那层包装可以简化掉。

新增的守卫都做了**变异验证**（把钩子改回 `Menu(callable)` → 确认测试报
`2 != 1` 变红 → 恢复 → 确认回绿）。过程中还发现：测试里的假图标若只把
「重建」做成一个计数器、没真去碰菜单对象，就**测不出**重复求值这件事 ——
假件复刻得不够像，守卫就是假的。已补正。

---

## 上一版（v0.1.5）

**删掉最后一批死代码，并补好「防止它回来」的守卫。** 功能与用法完全不变。

### 1. 删掉 BatteryPoller 的异步 RPC 模式

`request_due()` / `mark_request_sent()` / `note_timeout()` 这三个方法是给旧
controller 异步回灌电量用的。controller 在 v0.1.4 已删 ⇒ 它们**只剩定义、
唯一调用方是单测**。主路径走的是同步的 `due()` + `poll_once()`。

删代码时测试没有一刀切，而是按**断言意图**分了三类：

- **测死机制的（删 2 项）**：比如「在途不发第二条」—— 同步模式下根本没有这个语义
- **测活语义的（迁移到存活 API，3 项）**：睡眠退避、唤醒恢复、设备在线标记保持
  这些仍在生效，只是调用载体没了 ⇒ 改写成走 `due()` / `poll_response()`
- **补 1 项**：删掉的用例曾顺带覆盖「从未成功读过 + 读失败 ⇒ 判定睡眠」这条分支，
  补回来与「曾成功过则不判睡眠」成对

数量从 12 → 11，但**覆盖意图守恒** —— 没有哪条分支因为这次清理变成没人测。

### 2. 守卫补强（这是 v0.1.5 最实在的部分）

`tests/test_no_listener.py` 原来有两个洞，都已堵上并做了**变异验证**
（造违规 → 确认测试变红 → 清理 → 确认恢复绿）：

| 洞 | 修法 |
|---|---|
| 路径算错 / 没扫到文件 ⇒ 返回空列表 ⇒ **断言直接通过**（假通过） | 新增阳性自证：先断言扫描根存在、且扫到 >10 个 `.py` |
| 防复活只查顶层，`src/batbar/_legacy/wire.py` 即绕过 | 改成按文件名在**任意层级**匹配 |
| socket 检查只读 `agent.py`，docstring 却声称「整个包」 | 扫**全包**，与文字对齐 |

另加一条守卫：断言 `BatteryPoller` 上不再有那四个异步属性（能从 git 历史里
捞回来，故需守门）。

### 3. 文档与注释更正

一次代码审查发现，v0.1.4 的**代码删对了，但关于这次删除的叙述没跟上** ——
v0.1.5 一并修正：

- 因果写反了：原注释称「删 wire/net/controller 才没有 51788 监听」。**错** ——
  弹防火墙的是 `agent.py` 那个监听（v0.1.3 删）；`controller.py` 是 TCP **客户端**，
  三个文件从没监听过任何端口。注释把守卫指向了错误方向。
- README「诚实边界」表移除「桥接协议、分片鲁棒性」（那正是已删的测试）。
- Release 说明里「（见下）」指向的验收证据不存在 → 补上。
- 删掉 `setup-agent.cmd` 里一条**双重过期**的提示：它让用户去跑
  `scripts\firewall-agent.ps1`，而该文件在 git 历史里**从未存在过**，
  且 v0.1.3 起已无监听端口 ⇒ 防火墙规则根本不再需要。
- 数字同步：模块行数、CI 与文档里的单测数（**502 项**）。

### 上一版（v0.1.4）：把 v0.1.3「只删了一半」的旧架构清干净

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
全程不联网。

这点是**在真机上验过的**，不是推断：起 exe 后枚举该进程 PID 的**全部 TCP 端点**
（监听 + 已连接）为零。验收脚本 `tools/verify_no_listener.py` 自带**阳性对照** ——
先自己开一个监听确认枚举方法抓得到，才敢下「零」的结论（否则「没扫到」和
「真的没有」无法区分）。

#### 上三版（v0.1.3）：不再弹防火墙

删掉了旧架构遗留的本地 TCP 监听（`0.0.0.0:51788`）。这个端口早期用于
「Windows 侧 agent + WSL2 侧 controller」双进程通信，改单进程自跑后
controller 废弃，监听却留在 `agent.py` 里没一起删 ⇒ 每换一台机器、第一次
双击 exe，防火墙都要问一次「是否允许 batbar.exe 访问网络」。现在进程起来后
**零监听端口**，功能零影响。

#### 上四版（v0.1.2）：改成绿色（便携）模式

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
