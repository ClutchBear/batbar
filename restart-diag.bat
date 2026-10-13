@echo off
REM batbar restart (ASCII ONLY: cmd decodes .bat as GBK, UTF-8 Chinese
REM comments get mis-decoded and splice into the next command line).
REM
REM Why the kill step changed (2026-10-13):
REM   old:  powershell Get-CimInstance Win32_Process  -> full WMI process walk,
REM         measured 3~8s. That was the real reason "double-click the restart
REM         script, then wait 5~6s before the tray icon shows up".
REM   new:  the agent writes its PID to %LOCALAPPDATA%\batbar\agent.pid,
REM         so we taskkill it in tens of milliseconds. PowerShell is only a
REM         fallback when the pid file is missing.
setlocal
cd /d "D:\workbuddy\space\batbar"

set "PIDFILE=%LOCALAPPDATA%\batbar\agent.pid"
set PID=
if exist "%PIDFILE%" set /p PID=<"%PIDFILE%"

REM Timestamps: compare them with the agent log's "boot:" lines to see whether
REM the wait is inside the script, inside the process, or inside the taskbar.
echo [t] script start      %TIME%
echo [1/2] Killing any running batbar agent...
REM Only trust the pid file if that PID really is a python process (pid reuse /
REM stale file safety). Anything else -> slow fallback, because a live agent we
REM failed to kill would silently block the new one (single-instance mutex).
if not defined PID goto fallback
tasklist /FI "PID eq %PID%" /NH 2>nul | findstr /I /B "python" >nul
if errorlevel 1 goto fallback
echo   kill PID %PID%
taskkill /F /PID %PID% >nul 2>&1
goto dpi_check

:fallback
echo   (no live pid file - scan by image path)
powershell -NoProfile -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*batbar*' } | ForEach-Object { Write-Host ('  kill PID ' + $_.Id); Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }"
goto dpi_check

:dpi_check
echo [t] kill step done    %TIME%
REM optional arg1: system | pm2 | off  (sets BATBAR_DPI for this launch only)
if "%~1"=="" goto launch
set BATBAR_DPI=%~1
echo [2/2] Starting ONE fresh agent (BATBAR_DPI=%~1)...
goto do_launch

:launch
echo [2/2] Starting ONE fresh agent (default DPI=system)...

:do_launch
start "" ".venv\Scripts\python.exe" -m batbar.agent --standalone
echo [t] agent launched    %TIME%

echo.
echo Done. Icon should appear within ~1s - see "boot: icon registered" in the log.
echo.
pause
endlocal
