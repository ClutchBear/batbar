# Windows 开机自启注册：把 batbar agent 加进当前用户的启动计划任务。
# 用法（Windows PowerShell，无需管理员——只注册到当前用户）：
#   powershell -ExecutionPolicy Bypass -File scripts\register_autostart.ps1
# 删除：加参数 -Unregister
param([switch]$Unregister)

$taskName = "batbar-agent"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$py  = Join-Path $root ".venv\Scripts\python.exe"
$vbs = Join-Path $root "scripts\agent-hidden.vbs"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "[autostart] 已移除计划任务 $taskName"
    exit 0
}

if (-not (Test-Path $py)) {
    Write-Error "[autostart] 找不到 $py —— 请先运行 setup-agent.cmd"
    exit 1
}

# 用 wscript 跑 VBS 包装，避免计划任务弹出控制台窗口
$action  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero)   # 常驻进程不限执行时长（0=无限，PS5.1 用 -Infinite 亦可）

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "batbar: 多设备（鼠标 / 耳机 / 机械键盘）电量托盘 agent（由 WorkBuddy 项目生成）" | Out-Null

Write-Host "[autostart] 已注册开机自启：$taskName（登录时启动，崩溃自动重启 3 次）"
Write-Host "[autostart] 立即启动： Start-ScheduledTask -TaskName $taskName"
