"""蓝牙设备电量读取（Windows BTH*/GATT Battery Service，一次 CIM 查询）。

原理：BLE/经典蓝牙设备（耳机、三模键盘的 BT 通道）连上 Windows 后，系统蓝牙
栈解析其 Battery Service / HFP 电量上报，写进设备节点的 DEVPKEY_Device_BatteryLevel。
一次 Get-CimInstance 拿全量。

节点分布实测（本机）：
  QCY Era 7 耳机  -> BTHENUM{...111E...}（Hands-Free AG 节点带电量，裸设备节点没有）
  LEOBOG Hi98C Pro -> BTHLE\\DEV_xxx（GATT Battery Service 0x180F）
所以过滤必须用 LIKE 'BTH%'（覆盖 BTHENUM/BTHLE/BTHLEDEVICE），只查 BTHENUM 会漏键盘。

自适应过滤（2026-10 实测）：Windows 会**保留配对过设备的 PnP 节点**，键盘切去
2.4G dongle 后其 BTHLE 电量节点仍 Present=True、电量值还在——照单全收就会把
"已经不走蓝牙"的设备错误显示。判据分两路：
  BTHLE\\DEV_xxx（BLE 键鼠）：该 MAC 必须有**派生的活跃 HID 节点**
      （HIDClass 里 Present 且 DeviceID 含 _<MAC>&COL）才算"真在蓝牙上"。
      实测 Hi98C 切 2.4G 后派生 HID 节点消失 -> 正确剔除；K87 在 BT -> 保留。
  BTHENUM\\...（经典蓝牙耳机）：同 MAC 的**在连 profile 节点数 >= 2**
      （A2DP/AVRCP/HFP 成组出现才是活动连接；断连后整组消失）。
      实测两只耳机（QCY / XISEM-Air）保留，111E AG 电量节点判据通过。

★ 2026-10-12 晚新增**权威连接态过滤**（用户需求：未连接的设备不显示）：
  上面两路是「节点存在性」启发式，实测会漏——断连后节点照样驻留（XISEM 在
  充电盒里、两只键盘切到 2.4G，PnP 节点全部 Present/Status=OK/ProblemCode=0，
  电量值还留在缓存里）。真正可靠的是 WinRT `BluetoothDevice.ConnectionStatus`
  （Windows 设置页同款数据源，见 `_PS_CONN`）：实测 QCY=Connected，
  XISEM/Hi98C/K87=Disconnected，逐台与设置页一致。现在 `_parse` 拿到
  `connected` 表后**直接丢弃未连接的设备**；查询失败则退化为不过滤
  （fail-open），宁可多显示也不丢功能。

⚠️ 电量可读 ≠ 每轮都可读（2026-10-12 实测，改动缘由）：
  HFP-AG 节点的 DEVPKEY_Device_BatteryLevel 是**间歇性**的——西圣
  XISEM-Air 首次全量扫描读到 70%，随后连续三次查询都是 NULL。旧实现见到
  NULL 就 `return`，等于把设备整个丢掉，于是它在菜单里时有时无。
  现在改为：过滤只负责判断**在不在连**，电量读不到就如实报 None，由
  lastknown 模块补上「最后一次读到的值」（并在设备确实离线时才消失）。

⚠️ 诚实边界：2.4G 接收器模式不走 BTH*（LEOBOG 的 2.4G dongle 无标准电量上报，
读不到属预期），此时走 receiver24 + lastknown；部分耳机只走厂商私有协议
也不显示。读不到 = 不在此路径。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time

from . import lastknown
from . import winproc

log = logging.getLogger("batbar.bt")

# 单引号内是完整 PS 脚本；ConvertTo-Json 输出 UTF-8 JSON 数组。
# 自适应：先建两张表（活跃 HID 派生节点 MAC 集 + 同 MAC 在连 profile 计数），
# 再按设备类型二选一过滤，剔除"配对过但已切 2.4G / 已断连"的陈旧蓝牙节点。
_PS_QUERY = """\
$ErrorActionPreference='SilentlyContinue'
$hid = @{}
Get-CimInstance Win32_PnPEntity -Filter "PNPClass='HIDClass'" | Where-Object { $_.Present } | ForEach-Object {
  if ($_.DeviceID -match '_([0-9A-F]{12})&COL') { $hid[$matches[1]] = $true }
}
$prof = @{}
Get-CimInstance Win32_PnPEntity -Filter "DeviceID LIKE 'BTH%'" | Where-Object { $_.Present } | ForEach-Object {
  $d = $_.DeviceID; $m = ''
  if ($d -match 'DEV_([0-9A-F]{12})') { $m = $matches[1] }
  elseif ($d -match '&([0-9A-F]{12})_C00000000') { $m = $matches[1] }
  if ($m) { $prof[$m] = [int]$prof[$m] + 1 }
}
$r = @()
Get-CimInstance Win32_PnPEntity -Filter "DeviceID LIKE 'BTH%'" | ForEach-Object {
  if (-not $_.Present) { return }
  $d = $_.DeviceID; $m = ''
  if ($d -match 'DEV_([0-9A-F]{12})') { $m = $matches[1] }
  elseif ($d -match '&([0-9A-F]{12})_C00000000') { $m = $matches[1] }
  if (-not $m) { return }
  # 节点类型只精确取两类带电量的：BTHLE DEV_ 节点（BLE 键鼠 GATT 电池服务）与
  # BTHENUM 的 111E HFP-AG 节点（经典蓝牙耳机）。注意：枚举器把前缀小写化
  # （bthle），WQL 的 LIKE 不区分大小写，用大写前缀 StartsWith 过滤会全部漏掉。
  $up = $d.ToUpper()
  if ($up.StartsWith('BTHLE') -and $up.Contains('DEV_' + $m)) {
    if (-not $hid.ContainsKey($m)) { return }
  } elseif ($up.Contains('{0000111E-')) {
    if ([int]$prof[$m] -lt 2) { return }
  } else {
    return
  }
  # ⚠️ 这里**不能**因为电量读不到就 return：设备「在连」由上面的两路判据决定，
  #    电量读不到只是这一枪没打中，如实报 $null 交给调用方兜底。
  # ⚠️ 首次 Get-PnpDeviceProperty 常返回 NULL，连着读第二枪才出值
  #    （实测 2026-10-12 西圣 XISEM-Air：第 1 次 NULL，第 2~5 次稳定 70%）
  #    ——像是第一次调用才把这个属性的值拉进本机缓存。做「最多三枪、
  #    每枪间隔 200ms」的重试，能救回绝大多数抖动。
  #    ⚠️ 枪数由 __TRIES__ 注入（bug 13 提速）：「立即刷新」走单枪快查——
  #    它的使命是「在不在 + 大概电量」，在线设备首枪基本有值（偶尔 NULL
  #    由 lastknown 兜底），省下 400ms+ 的重试与属性调用；后台预热照旧
  #    三枪保数值精度。
  $tries = __TRIES__
  $bat = $null
  foreach ($try in 1..$tries) {
    $p = Get-PnpDeviceProperty -InstanceId $_.InstanceId `
          -KeyName 'DEVPKEY_Device_BatteryLevel' -ErrorAction SilentlyContinue
    if ($p -and $p.Data -ne $null) { $bat = [int]$p.Data; break }
    Start-Sleep -Milliseconds 200
  }
  $r += [pscustomobject]@{ name = $_.Name; id = $d; mac = $m; battery = $bat }
}
ConvertTo-Json -InputObject $r -Compress
"""

# ── 连接态查询（2026-10-12 晚新增，用户需求：未连接的蓝牙设备不显示）──
# PnP 节点属性**不可靠**：配对过的节点断连后仍 Present=True / Status=OK /
# ProblemCode=0，连电量值都留在缓存里（实测 K87/Hi98C/XISEM 三台「未连接」
# 却全被旧判据放行）。DevNodeStatus 也试过——AG 节点已连/未连都是 0x0180600A，
# 无区分度。
# 权威源 = WinRT `BluetoothDevice.ConnectionStatus`（Windows 设置页同款）：
# 实测 QCY=Connected，XISEM/Hi98C/K87=Disconnected，与设置页逐台一致。
# ⚠️ 必须用 Windows PowerShell 5.1 跑：PS7 默认没有 WinRT 类型投影。
_PS_CONN = """\
$ErrorActionPreference='SilentlyContinue'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $resultType) {
  $t = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
  return $t.Result
}
[void][Windows.Devices.Bluetooth.BluetoothDevice,Windows.Devices.Bluetooth,ContentType=WindowsRuntime]
[void][Windows.Devices.Bluetooth.BluetoothLEDevice,Windows.Devices.Bluetooth,ContentType=WindowsRuntime]
$o = @{}
foreach ($m in __MACS__) {
  try { $addr = [Convert]::ToUInt64($m, 16) } catch { continue }
  $conn = $false; $ok = $false
  # ① 经典蓝牙 API：耳机等经典设备权威（BLE HID 键盘对它假阴性）
  try {
    $dev = Await ([Windows.Devices.Bluetooth.BluetoothDevice]::FromBluetoothAddressAsync($addr)) ([Windows.Devices.Bluetooth.BluetoothDevice])
    $ok = $true
    if ($dev -ne $null -and [string]$dev.ConnectionStatus -eq 'Connected') { $conn = $true }
  } catch { }
  # ② BLE 专用 API：BLE HID 键盘权威；仅当①未报在连时才查（短路省时，同进程零额外开销）
  if ($ok -and -not $conn) {
    try {
      $le = Await ([Windows.Devices.Bluetooth.BluetoothLEDevice]::FromBluetoothAddressAsync($addr)) ([Windows.Devices.Bluetooth.BluetoothLEDevice])
      if ($le -ne $null -and [string]$le.ConnectionStatus -eq 'Connected') { $conn = $true }
    } catch { }
  }
  if ($ok) { $o[$m] = $conn }
}
ConvertTo-Json -InputObject $o -Compress
"""

# 连接态缓存 TTL：查询要起一个 5.1 进程（~1.5s），不必每轮都问
_CONN_TTL = 15.0
_conn_cache: tuple[float, dict[str, bool]] = (0.0, {})

# 空结果容忍窗：连续这么多次空查询才接受「设备真空了」（~60s）。
# WMI/WinRT 查询间歇性抽风（实测 2026-10-13 00:0x：连着的 LEOBOG/QCY 上一轮
# 还在、下一轮整表空），照单全收会让菜单在「有设备」和「未发现设备」之间
# 翻烙饼——图标与列表各读一次缓存就劈成两个世界。电量列表不会从 3 台瞬间
# 变 0 台，空结果几乎总是查询抖动而非真离线。
# 空结果宽限期（秒）。WMI/CIM 查询会间歇性整表返回空（实测：连着的
# LEOBOG/QCY 上一轮还在、下一轮全空），而**空结果返回极快**——不带宽限的
# 计数判据几秒就能攒够次数。
# ⚠️ 2026-10-13 01:18 用户实测：旧实现用「连续 EMPTY_LIMIT=3 次」当判据
#    （注释写着 ≈60s，实际 3 次只花了 7 秒）→ 缓存被清空 → 菜单「未发现
#    设备」而托盘图标还挂着旧数字（那一次图标更新又撞上 WinError 1402，
#    见 agent._set_icon 的锁）——「图标有、列表没有」就是这么来的。
# 现在改按时间：距上次**成功查询**不足 EMPTY_GRACE 就一律保留旧列表。
EMPTY_GRACE = 180.0

# 上线快判新增设备的「闪回保护期」（秒，bug 15）。
# `sync_now()` 靠 WinRT 连接态秒判设备上线并立即塞进 `_devices`，
# 但紧随其后的 CIM 电量查询这一轮**可能还没扫到它的行**（PnP 节点建立
# 滞后于 WinRT 连接态）——`_accept` 的整体替换会把刚加的设备抹掉，用户
# 看到「闪现又消失」。保护期内 CIM 没扫到就沿用快判给的 lastknown 兜底值；
# 一旦 CIM 扫到真实行（name in devs）保护自动失效。取 25s（> 20s TTL，
# 确保覆盖至少一次 prewarm 周期），设备若又断开则由 `sync_now` 的下线
# 分支摘除并清保护标记（不会被 _accept 复活）。
_FAST_ADD_PROTECT = 25.0

# 首次调用 `cached_devices()` 时，等预热线程的**上限**（秒）。
# 2026-10-16 修：原先是固定 `for _ in range(50): sleep(0.1)`（= 最多 5s），
# 而调用方是 pystray **同步**建的右键菜单 ⇒ 启动后第一次点开菜单被冻住 5 秒，
# 等满了若仍无结果照样返回空表（蓝牙条目整批消失）。改成 2s + last-known 兜底。
_WAIT_FIRST = 2.0


def connected_macs(macs: list[str], *, force: bool = False) -> dict[str, bool]:
    """{MAC: 是否已连接}；查不到返回 {}（调用方按「未知」处理，即不过滤）。

    两条 WinRT 判据在**同一个 PS 进程**里取并集（2026-10-13 13:20 定稿，
    见 `_PS_CONN`）：
      ① `BluetoothDevice.ConnectionStatus`（经典蓝牙）—— 耳机权威
         （实测 QCY=Connected / XISEM=Disconnected 与设置页一致）；
      ② `BluetoothLEDevice.ConnectionStatus`（BLE）—— BLE HID 键盘权威
         （实测 LEOBOG 在连=True；离线 BLE 设备 DBA28E=False，不陈旧）。
      ①报在连就短路不查②（省时）；①假阴性时②兜住 BLE。

    ⚠️ 取代了旧的「hid 活跃派生节点」补证（2026-10-12）：hid 判据
    (`Win32_PnPEntity PNPClass=HIDClass ... Present`) 要**另起一个 ~2s 的
    CIM 进程**，且蓝牙 HID 设备**关机后节点不会立刻移除**（Present 仍 True
    一段时间）→ 把已关机误判成在连，「立即刷新」快判失效、退化成等 CIM
    全量查询（~3-4s，用户 13:14 报「蓝牙关机等三四秒才消失」）。`le` API
    同进程零额外开销、关机即报 Disconnected，全面胜出。

    纯 I/O，失败一律吞掉——连接态查询挂掉不能让蓝牙电量整个不可用。
    """
    global _conn_cache
    now = time.monotonic()
    if not force and now - _conn_cache[0] < _CONN_TTL:
        cached = _conn_cache[1]
        if all(m in cached for m in macs):
            return cached
    if not macs:
        return _conn_cache[1]
    shell = shutil.which("powershell")  # 必须 5.1（WinRT 投影）
    if not shell:
        return {}
    macs_lit = ",".join("'%s'" % m.upper() for m in macs)
    script = _PS_CONN.replace("__MACS__", "@(%s)" % macs_lit)
    try:
        r = winproc.run_hidden(
            [shell, "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=25,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("bt conn query failed: %s", exc)
        return {}
    out = (r.stdout or "").strip()
    if not out or out == "null":
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        log.debug("bt conn json fail: %r", out[:120])
        return {}
    if not isinstance(data, dict):
        return {}
    res = {str(k).upper(): bool(v) for k, v in data.items()}
    # 连接态判据（经典 classic + BLE le）已在 `_PS_CONN` 脚本内合并完成，
    # Python 侧不再做 hid 派生节点补证——那套要另起 ~2s CIM 进程且关机后
    # 陈旧（节点 Present 不及时移除），会把已关机误判成在连（bug 14）。
    # 合并进缓存（不同调用方可能只关心部分 MAC）
    _conn_cache = (now, {**_conn_cache[1], **res})
    return res


# 关键字 → 友好名（小写匹配，先命中先赢）。# 命名规范（用户拍板 2026-10-12）：「类别 品牌+型号」—— 耳机/鼠标/键盘打头，
# 后面跟能确定的具体型号；型号未知的保留品牌泛称。
_NICKNAMES = [
    ("hi98", "键盘 LEOBOG Hi98C Pro"),
    ("leobog", "键盘 LEOBOG Hi98C Pro"),
    ("qcy", "耳机 QCY Era 7"),
    ("era 7", "耳机 QCY Era 7"),
    ("airpods", "耳机 AirPods"),
    ("wh-1000", "耳机 Sony"),
    ("wf-1000", "耳机 Sony"),
    ("xisem", "耳机 西圣 XISEM-Air"),
    ("galaxy buds", "耳机 三星 Buds"),
    ("freebuds", "耳机 华为 FreeBuds"),
    ("xiaomi", "耳机 小米"),
    ("nuphy", "键盘 NuPhy"),
    ("machose", "键盘 迈从 K87"),
    ("mchose", "键盘 迈从 K87"),
    ("rapoo", "雷柏"),
    ("vgn", "鼠标 VGN"),
    ("atk", "鼠标 ATK"),
    ("vxe", "鼠标 VXE"),
]

# 设备节点名里的协议噪声后缀，友好化时剥掉
_NOISE = (
    " Hands-Free AG",
    " Hands-Free",
    " Avrcp 传输",
    " 蓝牙 LE 链路层",
    " BLE",
    " 5.0",
    " 5.1",
    " 5.2",
    " 5.3",
    " 5.4",
)


def friendly(name: str) -> str:
    """把 PnP 设备名转成托盘菜单用的短名。"""
    low = name.lower()
    for kw, nick in _NICKNAMES:
        if kw in low:
            return nick
    out = name
    for suf in _NOISE:
        out = out.replace(suf, "")
    return out.strip() or name


def _pwsh() -> str | None:
    """找 PowerShell 7（pwsh），找不到退回 Windows PowerShell 5.1。"""
    p = shutil.which("pwsh")
    if p:
        return p
    return shutil.which("powershell")


def _parse(items, connected: dict[str, bool] | None = None) -> dict[str, int | None]:
    """把 PowerShell 的 JSON 解析成 {友好名: 电量%|None}。

    纯函数（不碰 lastknown、不起进程），便于单测。

    Args:
        connected: {MAC(大写): 是否已连接}。**未连接的行直接丢弃**（用户需求
            2026-10-12：未连接的蓝牙设备不显示）。MAC 不在表里 = 未知 →
            不过滤（fail-open，连接态查询失败时仍保留原有功能）。

    合并规则：
      * 同名多节点取**有读数的那个**（值通常相同，取最后一条非空值）；
      * 同名但全为 None 时保留 None —— 代表「在连但这一轮没读到」。
    """
    devs: dict[str, int | None] = {}
    for d in items or []:
        if not isinstance(d, dict):
            continue
        mac = str(d.get("mac") or "").upper()
        if connected is not None and mac and connected.get(mac) is False:
            continue  # 未连接：整条不显示
        name = friendly(str(d.get("name") or "BT"))
        raw = d.get("battery")
        pct = None if raw is None else max(0, min(100, int(raw)))
        if pct is not None:
            devs[name] = pct
        elif name not in devs:
            devs[name] = None  # 有读数的节点永远优先，不被 None 覆盖
    return devs


class BluetoothBattery:
    """一次 CIM 查询缓存全部蓝牙设备电量（TTL 复用，避免频繁起进程）。

    all_devices() 返回 {友好名: 电量%|None}：
      * 有读数 -> 当轮真值，同时写进 lastknown（供 2.4G 模式与 NULL 轮复用）；
      * 读不到 -> 用 lastknown 里最后一次读数兜底；真没记录才给 None。
    """

    def __init__(self, ttl: float = 20.0):
        self._ttl = ttl
        self._ts = 0.0
        self._devices: dict[str, int | None] = {}  # 友好名 -> 电量%
        self._shell = None
        self._refreshing = False  # 后台预热去重（多个调用方同时触发）
        self._lock = threading.Lock()  # 并发线程同时判定 TTL 时防重复起进程
        self._iolock = threading.Lock()  # 真正的查询串行化（防两个 PS 并存）
        self._empty_streak = 0  # 连续空结果计数（仅用于日志）
        self._last_ok_ts = 0.0  # 上次拿到非空列表的时刻（宽限期基准）
        self._mac_by_name: dict[str, str] = {}  # 友好名 -> MAC（空表复核用）
        self._fast_add_protect: dict[str, float] = {}  # 友好名 -> 保护截止（上线快判闪回保护，bug 15）

    def prewarm(self) -> None:
        """后台预热：TTL 到期就另起线程刷新，主线程（菜单）不被拖住。

        CIM 查询实测约 4s（每个节点最多三枪电量重试），而 pystray 右键菜单是
        **同步**建的 *_menu_items* ——撞上冷缓存会明显卡顿。这里由常驻线程
        提前把缓存焐热，菜单按需直接吃旧值（电量秒级不变，旧几十秒无感）。
        """
        with self._lock:
            if self._refreshing:
                return
            if time.monotonic() - self._ts <= self._ttl:
                return
            self._refreshing = True

        def _run() -> None:
            try:
                self._refresh()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._refreshing = False

        threading.Thread(target=_run, daemon=True).start()

    def _refresh(self) -> None:
        # I/O 串行化：预热线程与调用方可能同时判定 TTL 到期，不加锁会并发
        # 起两个 PowerShell（各 ~4s），纯浪费。
        with self._iolock:
            self._run_query()

    def _run_query(self, force_conn: bool = False, quick: bool = False) -> None:
        """跑一次查询并更新缓存；由 _refresh 串行调用。

        Args:
            force_conn: True = 连接态表也强制重查（「立即刷新」用）——耳机
                刚连上时 WinRT 可能还报 Disconnected，15s 连接态缓存会把
                它错杀掉，用户点了刷新却看不到，必须绕过缓存。
            quick: True = 电量读数走**单枪**（跳过 200ms 重试）。「立即刷新」
                的使命是「在不在 + 大概电量」，在线设备首枪基本有值（偶尔
                NULL 由 lastknown 兜底），省 ~0.5-1s；后台预热照旧三枪保
                数值精度（bug 13 提速）。
        """
        if self._shell is None:
            self._shell = _pwsh() or False
        if not self._shell:
            log.warning("未找到 pwsh/powershell，蓝牙电量源不可用")
            return
        script = _PS_QUERY.replace("__TRIES__", "1" if quick else "3")
        try:
            r = winproc.run_hidden(
                [self._shell, "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=25,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("bt query failed: %s", exc)
            return
        out = (r.stdout or "").strip()
        data: list = []
        if out and out != "null":
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                # 解析失败 = 这一枪没打中。不动缓存、不更新 _ts，
                # 下一轮 TTL 到期自然重试（保留旧列表好过翻烙饼）。
                log.debug("bt json parse fail: %r", out[:120])
                return
            if isinstance(data, dict):  # 单条时 ConvertTo-Json 可能输出裸对象
                data = [data]
        # 空输出（""/null）按「无行」处理，走 _accept 的保留逻辑，不再直接清缓存。
        # 连接态过滤（未连接的蓝牙设备不显示）：只对候选 MAC 查一次
        macs = [str(d.get("mac") or "").upper() for d in data if isinstance(d, dict) and d.get("mac")]
        conn = connected_macs(sorted(set(macs)), force=force_conn) if macs else {}
        devs = _parse(data, connected=conn or None)

        # 记住「友好名 → MAC」：空表复核（_all_offline_confirmed）要靠它
        # 反查 WinRT 连接态——CIM 整表空的时候行里没有 MAC，只能吃这里
        # 上次成功查询攒下的映射。
        for d in data:
            if isinstance(d, dict) and d.get("mac"):
                self._mac_by_name[friendly(str(d.get("name") or "BT"))] = str(d["mac"]).upper()

        # 记住真值；读不到这一轮的设备用历史值兜底
        for name, pct in list(devs.items()):
            if pct is not None:
                lastknown.put(name, pct)
            else:
                got = lastknown.get(name)
                devs[name] = None if got is None else got[0]
        self._accept(devs)

    def _accept(self, devs: dict[str, int | None]) -> None:
        """查询结果落缓存；**空结果在宽限期内不清空旧列表**——除非复核过。

        判据是**时间**不是次数（2026-10-13 01:18 用户实测教训）：
        WMI 查询间歇性返回空表，而空结果返回极快 —— 旧实现「连续 3 次空」
        的判据 7 秒就满足了，缓存被清 → 弹菜单读到空 = 「未发现设备」，
        可托盘图标还挂着旧数字（图标那次更新又失败了）→ 用户看到
        「托盘有电量、列表没有」。现在只要距上次**成功查询**不足
        `EMPTY_GRACE` 秒就一律保留旧列表；只有空结果持续超过宽限期，
        才接受「设备真的全下了」。

        ⚠️ 例外（2026-10-13 11:41 bug 12：关掉**最后一台**蓝牙设备后，
        「立即刷新」和等待都杀不掉条目）：空表分两种——「查询抖动」（设备
        还在、CIM 抽风整表空）与「真的全下线」。不区分就卡满 180s 宽限；
        而设备还剩 ≥1 台时关掉其中一台，查询结果非空、不进宽限，4-5s 就
        消失——这正是用户看到的两场景差异。复核判据：拿上一轮在连设备的
        MAC 问 WinRT 连接态（`connected_macs`，设置页同源、不受 CIM 空表
        影响），**全部确认离线 = 空表为真 → 立即接受**；问不出（{}）或
        仍有在连 = 复核不可信/空表必是抖动 → 维持宽限（抖动防护不丢）。
        """
        now = time.monotonic()
        if devs or not self._devices:
            self._empty_streak = 0
            self._last_ok_ts = now
            self._devices = self._preserve_fast_adds(devs)
        elif now - self._last_ok_ts >= EMPTY_GRACE:
            self._empty_streak = 0
            self._devices = {}
            log.info(
                "bt 空结果已持续 %.0fs（> %.0fs 宽限），接受为空（设备真离线）",
                now - self._last_ok_ts,
                EMPTY_GRACE,
            )
        elif self._all_offline_confirmed():
            self._empty_streak = 0
            log.info(
                "bt 空结果 + WinRT 复核全部离线（距上次成功 %.0fs），提前接受为空（最后一台蓝牙已断开）",
                now - self._last_ok_ts,
            )
            self._devices = {}
        else:
            self._empty_streak += 1
            log.info(
                "bt 查询空结果（连续第 %d 次，距上次成功 %.0fs），保留上次列表 %s",
                self._empty_streak,
                now - self._last_ok_ts,
                sorted(self._devices),
            )
        self._ts = now

    def _preserve_fast_adds(self, devs: dict[str, int | None]) -> dict[str, int | None]:
        """闪回保护（bug 15）：CIM 整体替换 `_devices` 时，保住快判刚加的、
        这一轮 CIM 还没扫到的设备（PnP 节点建立滞后于 WinRT 连接态）。

        - 保护期内 + CIM 本轮**没**扫到该设备 → 沿用上一轮 `_devices` 里的
          兜底值（`sync_now` 用 lastknown 填的），不删；
        - CIM 本轮**扫到了**（name in devs）→ 保护自动失效（以 CIM 真实读数为准）；
        - 保护期已过 → 移除标记，交给常规逻辑（`sync_now` 下线分支若它
          又离线会摘掉）。

        返回合并后的 dict（不就地改 devs）。
        """
        if not self._fast_add_protect:
            return devs
        now = time.monotonic()
        out = dict(devs)
        for name in list(self._fast_add_protect):
            if name in devs:  # CIM 扫到了 → 保护失效
                del self._fast_add_protect[name]
                continue
            if now >= self._fast_add_protect[name]:  # 保护期已过
                del self._fast_add_protect[name]
                out.pop(name, None)
                continue
            if name not in out:  # 沿用快判兜底值，别抹掉
                prev = self._devices.get(name)
                out[name] = prev
                log.debug("上线快判保护：%s 本轮 CIM 未扫到，沿用兜底值 %s", name, prev)
        return out

    def _all_offline_confirmed(self) -> bool:
        """空表复核：上一轮在连的设备在 WinRT 连接态里**全部**离线？

        Returns:
            True  = 权威源确认全离线，空表可信，可立即清空；
            False = 连接态问不出（失败/超时，fail-safe 维持宽限）或仍有
                    设备报「已连接」（那空表必是 CIM 抖动）。

        现场取证（2026-10-13 11:41 `probes/probe_bt_last_device.py`）：键盘
        关机后 `_PS_QUERY` 整表空，但 WinRT 对其 MAC 如实报 false——
        这正是把「真离线」从「查询抖动」里摘出来的权威信号。
        """
        macs = [self._mac_by_name.get(n) for n in self._devices]
        macs = [m for m in macs if m]
        if not macs:  # 没有任何 MAC 可问 → 无法复核
            return False
        # 宽限里可能要撑 180s，连接态不能吃 15s 旧缓存（stale 的 Connected
        # 会把真离线错判成抖动），必须 force=True。
        conn = connected_macs(macs, force=True)
        if not conn:  # 查询失败 = 未知，维持宽限
            return False
        return not any(conn.get(m) for m in macs)

    def force_refresh(self, quick: bool = False) -> None:
        """「立即刷新」专用：同步强刷一次（无视 20s TTL），连接态也重查。

        CIM 查询 ~4s，绝不能在常规轮询里调（只有用户显式点刷新才值得等）。
        与 prewarm 的差别：prewarm 在 TTL 内直接返回，且连接态吃 15s 缓存
        ——刚连上的耳机因此可能 15~20s 进不了列表，用户点了刷新却看不到。

        Args:
            quick: True = 电量读数单枪快查（bug 13）。手动刷新的「上线加入 /
                离线摘除」已由 `sync_now()`（WinRT 连接态双向快判，~0.6s）
                先行完成，这一步只负责电量值刷新与新设备发现，越快越好。
        """
        with self._iolock:
            self._run_query(force_conn=True, quick=quick)

    def drop_disconnected_now(self) -> list[str]:
        """「立即刷新」前置快判（bug 13）：WinRT 连接态确认离线的条目
        **立即摘除**，不等 ~2s 的 CIM 电量查询——用户点刷新到条目消失
        的时延从 3-4s 压到 ~0.6s。

        安全性：摘除判据是权威连接态（classic ∪ le，见 `connected_macs`），
        真在连的设备绝不误摘；即使极端情况下摘错，下一轮 CIM 查询也会
        把设备加回来（fail-open，快照每轮重算）。

        Returns:
            本轮摘除的友好名列表（空 = 没摘任何东西）。
        """
        with self._iolock:
            if not self._devices or not self._mac_by_name:
                return []
            pairs = [(n, self._mac_by_name[n]) for n in sorted(self._devices) if n in self._mac_by_name]
            macs = sorted({m for _, m in pairs})
            if not macs:
                return []
            conn = connected_macs(macs, force=True)  # 不能吃 15s 旧缓存
            if not conn:  # 问不出 = 未知，不动缓存
                return []
            dead = [n for n, m in pairs if conn.get(m) is False]
            for n in dead:
                self._devices.pop(n, None)
                self._fast_add_protect.pop(n, None)  # 清保护标记（bug 15）
            if dead:
                log.info("bt 立即刷新：连接态确认离线，先摘 %s（电量值随后 CIM 复核）", dead)
            return dead

    def sync_now(self) -> tuple[list[str], list[str]]:
        """「立即刷新」双向快判（bug 15）：**一次** WinRT 查询同时处理
        上线与下线——对称且不退化（若分别调 add + drop 两次，会各 spawn
        一次 ~0.6s，下线时延翻倍）。

        对 `_mac_by_name`（友好名 → MAC，只增不减）里的**全部** MAC 批量问
        连接态（classic ∪ le，~0.6s 同进程），逐台和解：
          - 在列表 + 报离线 → 摘除（= drop_disconnected_now 的效果）；
          - 不在列表 + 报在连 → 加入（lastknown 兜底 + 闪回保护）；
          - 报在连且在列表 / 报离线且不在列表 → 不动；
          - 未知（查询失败）→ 全不动（fail-open）。
        `_devices` 里没有对应 MAC 的条目一律保留（无法判定，不碰）。

        候选来源 = `_mac_by_name`（只增不减：设备断开后映射仍在，bug 12 的
        空表复核就依赖这个特性）。局限：app 重启后 `_mac_by_name` 为空
        （纯内存），**首次**开机连接的设备走不了快判，仍靠 CIM 全量查询发现
        （~2-4s）；一旦查询跑过一次映射就位，此后「关闭→打开→刷新」全程
        秒级（agent 常驻，覆盖用户场景）。

        Returns:
            (本轮新增友好名列表, 本轮摘除友好名列表)。
        """
        with self._iolock:
            if not self._mac_by_name:
                return [], []
            all_macs = sorted(set(self._mac_by_name.values()))
            conn = connected_macs(all_macs, force=True)  # 不能吃 15s 旧缓存
            if not conn:  # 问不出 = 未知，全不动（fail-open）
                return [], []
            now = time.monotonic()
            added: list[str] = []
            dropped: list[str] = []
            for name, mac in self._mac_by_name.items():
                state = conn.get(mac)
                if state is None:  # 这台没问到 → 不动
                    continue
                in_list = name in self._devices
                if state and not in_list:  # 刚上线 → 加入
                    rec = lastknown.get(name)
                    self._devices[name] = None if rec is None else rec[0]
                    self._fast_add_protect[name] = now + _FAST_ADD_PROTECT
                    added.append(name)
                elif not state and in_list:  # 已离线 → 摘除
                    self._devices.pop(name, None)
                    self._fast_add_protect.pop(name, None)
                    dropped.append(name)
            if added or dropped:
                log.info(
                    "bt 立即刷新：连接态双向快判 → 上线 %s / 下线 %s（电量值随后 CIM 刷新）",
                    sorted(added),
                    sorted(dropped),
                )
            return added, dropped

    def all_devices(self) -> dict[str, int | None]:
        """{设备名: 电量%|None}；TTL 内复用缓存。"""
        if time.monotonic() - self._ts > self._ttl:
            self._refresh()
        return dict(self._devices)

    def cached_devices(self) -> dict[str, int | None]:
        """不阻塞版：直接给缓存值，并顺手触发后台预热。

        ⚠️ 2026-10-16 修（"第一次调用必须同步等一次"这条例外被取消）：
        本方法的调用方是 `agent.devices_snapshot()`，而它被 pystray 的菜单
        工厂**同步**调用；CIM 全量查询实测 ~4s，预热线程在跑时又要空转最多
        5s —— 合起来就是「启动后第一次点开托盘菜单卡住 4~5 秒」，卡完若还没
        结果照样返回空表（蓝牙条目整批消失，要等下一轮预热才补回来）。
        现在首次也交给后台预热，最多等 `_WAIT_FIRST` 秒，超时用 last-known
        里最后读到的值顶上（宁可旧值也别闪空 —— 与 `EMPTY_GRACE`、闪回保护
        是同一套产品取向）。
        """
        if self._ts == 0.0:
            if self._refreshing:
                self._await_first()  # 已经有人在查了：等它，别再起一个 PS
            else:
                self.prewarm()  # 没人查：后台起一个，本轮不吃这 4s
            if not self._devices:  # 还没结果 → 用上次读到的值先顶上
                return self._lastknown_snapshot()
        else:
            self.prewarm()
        return dict(self._devices)

    def _await_first(self) -> None:
        """等预热线程出结果（上限 `_WAIT_FIRST` 秒，50ms 粒度）。"""
        deadline = time.monotonic() + _WAIT_FIRST
        while time.monotonic() < deadline:
            if self._ts != 0.0:
                return
            time.sleep(0.05)

    def _lastknown_snapshot(self) -> dict[str, int | None]:
        """冷启动兜底：{友好名: 上次读到的电量}。

        值可能旧（甚至设备已经离线），但比返回空表好：空表会让托盘直接跳到
        「未发现设备」，而这几秒后真查询的结果就会覆盖它。
        """
        try:
            return {n: pct for n, (pct, _ts) in lastknown.all_records().items()}
        except Exception as exc:  # noqa: BLE001 - 兜底失败就当没兜底
            log.debug("last-known fallback failed: %s", exc)
            return {}
