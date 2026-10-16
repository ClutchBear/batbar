## 下载

**`batbar-*-win64.zip`** —— 解压即用，**不需要装 Python**，不需要任何运行时。

1. 解压到任意位置（例如 `D:\batbar\`）
2. 双击 `batbar.exe` —— 它会常驻系统托盘（**没有窗口，属正常**）
3. 退出：托盘图标右键 → 退出

压缩包里还附了 `使用说明.txt`，含配置项与故障排查。

## 本版变化（v0.1.8）

**一次代码审查 + 四个修复。没有新功能，用法完全不变。**

修的全是「平时不触发、真触发就是大坑」的类型：句柄泄漏、扫描白等 3 秒、
首次查询卡菜单 4 秒。另有一项顺带的性能提升（2.4G 批量扫描快 3 倍）。

### 1. 修 2.4G 接收器句柄泄漏（本版核心）

`transport.py` 里 `close()` 被**写了两次**——Python 后定义覆盖前定义，
**不报错、ruff 也查不出**（重复定义不是语法错误）。活下来的版本只关
主句柄，忘了关按 report ID 拆出来的兄弟 collection。

后果不是「关不干净」这么轻：罗技接收器按 report ID 把输入拆成多个
兄弟 collection，主句柄之外的那些**没人关**。托盘常驻、每次设备枚举
都走一遍开→关，句柄就一路漏到进程句柄表涨满——到那天 `OpenDevice`
全部失败，托盘「看着还活着、电量永远不更新」。

修法只有一行：删掉被覆盖的旧定义，让真正遍历 `_readers` 的版本活下来。
泄漏验证走真类 + 假设备：修前 `泄漏 ['reader1','reader2']`，修后 `leaked = []`。

**守卫（tests/test_transport_close.py）**：除功能用例外，还加了**源码扫描
守卫**——AST 扫全仓，任何类里出现同名方法定义就直接红。这条守卫自带：

- **阳性对照**：把旧 bug 喂回去 → 确认测试变红（不然等于没守）
- **阴性对照**：不同类里的同名方法不许误报（不同类同名是正常写法）
- **自证**：先断言扫描根存在且扫到 >10 个 `.py`——扫描不到文件时断言
  「通过」是假通过，必须先排除这种可能

### 2. 2.4G 批量扫描：4 个设备 4.5s → 1.5s

原实现**每个设备等一次稳定窗口**：4 个设备就是 4 × 0.5s 的串行等待。
改成**一次共享等待窗口**：先记下全部设备的上一帧时间戳 → 一次性查询
所有设备 → 只等一个窗口 → 各自比对「帧时间戳有没有前进」。

正确性不是猜的：每个设备的基线时间戳在**它自己的帧发出之前**采集，
别的设备的帧不可能污染它的判定——逐设备验证仍然独立。

### 3. 蓝牙首次查询不再卡菜单 4 秒

`cached_devices()` 第一次被调用（缓存还是空的）时会**同步等**一个
PowerShell CIM 查询（实测 4s+），而这个函数是在**托盘线程**里同步建
菜单的工厂里调的——pystray 的 `Menu.items` 是 property，每访问一次就
重跑一遍工厂，于是右键弹菜单直接卡住，且等不到结果时还可能弹个**空表**。

修法三步：

1. 首次调用：没人在查就 `prewarm()` 后台起查询（本轮不等，先返回旧值）；
   已经有人查了就**有界等待**（最多 2s）
2. 等不到结果：返回 last-known 快照（上次读到的电量）先顶上，不是空表

### 4. 全仓 ruff format（86 个 .py）

纯格式、零逻辑改动。顺带说明：ruff 0.17 会**连 Markdown 里的代码块
一起格式化**（这是它的默认行为），所以 `更新记录.md` 的代码块也重排了。

### 5. 测试 518 → 532（+14）

| 新增 | 项数 |
|---|---:|
| transport 句柄泄漏（含源码扫描守卫 4 项） | 8 |
| poll24 共享等待窗口 | 2 |
| bt 首次查询有界等待 + last-known 兜底 | 4 |

全部绿。**硬件相关**的确认仍需本机跑：
`python -m batbar.agent --probe --allow-multiple` → 电量 90%。

---

## 上一版（v0.1.7）

**onefile 从 16.28 MB 瘦到 9.19 MB（−43%），功能不变。**

### 1. 去掉 OpenSSL 与无用的 Pillow 插件（本版核心）

batbar 全程不联网，却被 `logging.handlers` 带进了 OpenSSL（`libcrypto` 7.49 MB +
`libssl` 1.50 MB）；Pillow 的 AVIF 插件又占 7.53 MB，而我们只画 PNG 托盘图标。
现已：

- 用自写滚动日志 handler 替代 `logging.handlers.RotatingFileHandler`，从源码断掉
  `logging.handlers → ssl` 依赖链。
- 在打包 spec 排除 `xml.sax` 子树（PyInstaller 的 xml hook 会误收整个 SAX，
  再沿 `urllib.request → http.client → ssl` 把 OpenSSL 带回来）。`xml.etree.*`
  保留，Pillow 依赖不受影响。
- 剔除 AVIF / WebP / CMS 三个未使用的 Pillow 插件。
- 排除静态链接 `libcrypto` 的 `_hashlib` 扩展。Python 内置 `_md5` / `_sha1` /
  `_sha2` / `_sha3` / `_blake2` 提供 hashlib 降级；batbar 与依赖只用到
  `random` 的 sha512，不用 pbkdf2/scrypt。

| | v0.1.6 | v0.1.7 |
|---|---:|---:|
| EXE | 16.28 MB | **9.19 MB** |
| 工作集 | 59.5 MB | 59.0 MB |
| 专用提交 | 27.1 MB | 25.5 MB |

体积砍的是运行时**未加载**的 DLL，所以内存几乎不变；平均 CPU 同条件 1.00% →
0.94%（单核口径），差异在托盘程序的采样波动内。

### 2. 日志滚动语义保持不变

仍是 1 MB × 2 份，UTF-8；滚动顺序 `agent.log → agent.log.1 → agent.log.2`。
补了两项滚动链测试与一组网络依赖守卫（共 **518 项**）。

---

## 上两版（v0.1.6）

### 1. 右键菜单不再把硬件探测跑两遍（v0.1.6 核心）

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

## 上三版（v0.1.5）

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

### 上四版（v0.1.4）：把 v0.1.3「只删了一半」的旧架构清干净

v0.1.3 删掉了 `agent.py` 里的监听代码，但「双进程 + 本地 TCP 桥」那套的
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

#### 上五版（v0.1.3）：不再弹防火墙

删掉了旧架构遗留的本地 TCP 监听（`0.0.0.0:51788`）。这个端口早期用于
「Windows 侧 agent + WSL2 侧 controller」双进程通信，改单进程自跑后
controller 废弃，监听却留在 `agent.py` 里没一起删 ⇒ 每换一台机器、第一次
双击 exe，防火墙都要问一次「是否允许 batbar.exe 访问网络」。现在进程起来后
**零监听端口**，功能零影响。

#### 上六版（v0.1.2）：改成绿色（便携）模式

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
