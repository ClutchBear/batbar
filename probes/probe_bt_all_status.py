# -*- coding: utf-8 -*-
"""bug 14 全景取证：枚举所有配对蓝牙设备，对每台对比四个连接态源。

目的：在设备**真关机**时，判断哪个源能准确、不陈旧地报「离线」。
  - pnp_present : Win32_PnPEntity 节点是否存在（已知会陈旧）
  - hid_active  : 是否有 Present 的 HIDClass 派生节点（当前补证判据，疑陈旧）
  - classic     : BluetoothDevice.ConnectionStatus（经典蓝牙权威；BLE 假阴性）
  - le          : BluetoothLEDevice.ConnectionStatus（BLE 权威？待验证关机报什么）

用法：先在设备**开着**时跑一次（基线），关掉设备后再跑一次（对照）。
"""

import json
import subprocess
import time

_PS = r"""
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

# 1) hid 活跃 MAC 全集（复用生产判据：Present 的 HIDClass 派生节点）
$hidSet = @{}
Get-CimInstance Win32_PnPEntity -Filter "PNPClass='HIDClass'" | Where-Object { $_.Present } | ForEach-Object {
  if ($_.DeviceID -match '([0-9A-F]{12})&COL') { $hidSet[$matches[1]] = $true }
}

# 2) 枚举所有配对蓝牙设备，提取唯一 MAC
$macName = @{}
$macPnp  = @{}
Get-PnpDevice | Where-Object { $_.InstanceId -match 'BTHENUM|BTHLEDEVICE' } | ForEach-Object {
  if ($_.InstanceId -match '_([0-9A-Fa-f]{12})\\') {
    $mac = $matches[1].ToUpper()
    if (-not $macName.ContainsKey($mac)) { $macName[$mac] = $_.FriendlyName }
    if ($_.Present) { $macPnp[$mac] = $true } else { if (-not $macPnp.ContainsKey($mac)) { $macPnp[$mac] = $false } }
  }
}

# 3) 每个 MAC 测 classic / le
$out = @()
foreach ($mac in $macName.Keys) {
  $addr = [Convert]::ToUInt64($mac, 16)
  $classic = "n/a"; $le = "n/a"
  try {
    $d1 = Await ([Windows.Devices.Bluetooth.BluetoothDevice]::FromBluetoothAddressAsync($addr)) ([Windows.Devices.Bluetooth.BluetoothDevice])
    $classic = if ($d1 -eq $null) { "null" } else { [string]$d1.ConnectionStatus }
  } catch { $classic = "ERR" }
  try {
    $d2 = Await ([Windows.Devices.Bluetooth.BluetoothLEDevice]::FromBluetoothAddressAsync($addr)) ([Windows.Devices.Bluetooth.BluetoothLEDevice])
    $le = if ($d2 -eq $null) { "null" } else { [string]$d2.ConnectionStatus }
  } catch { $le = "ERR" }
  $out += @{
    mac     = $mac
    name    = $macName[$mac]
    pnp     = [bool]$macPnp[$mac]
    hid     = [bool]$hidSet[$mac]
    classic = $classic
    le      = $le
  }
}
ConvertTo-Json -InputObject $out -Compress
"""

t0 = time.perf_counter()
r = subprocess.run(
    ["powershell", "-NoProfile", "-Command", _PS],
    capture_output=True,
    text=True,
    timeout=90,
    encoding="utf-8",
    errors="replace",
)
t1 = time.perf_counter()

print("耗时 %.2fs" % (t1 - t0))
if (r.stderr or "").strip():
    print("STDERR:", r.stderr.strip()[:300])
out = (r.stdout or "").strip()
try:
    data = json.loads(out) if out else []
except json.JSONDecodeError:
    print("RAW:", out[:800])
    raise SystemExit(1)
if isinstance(data, dict):
    data = [data]

print("\n%-14s %-26s %-5s %-5s %-12s %-12s" % ("MAC", "NAME", "pnp", "hid", "classic", "le"))
print("-" * 80)
for d in data:
    print(
        "%-14s %-26s %-5s %-5s %-12s %-12s"
        % (
            d.get("mac", ""),
            (d.get("name", "") or "")[:24],
            d.get("pnp"),
            d.get("hid"),
            d.get("classic"),
            d.get("le"),
        )
    )
