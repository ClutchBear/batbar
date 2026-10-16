@echo off
REM batbar restart with DPI mode from arg1 (ASCII ONLY - cmd reads .bat as GBK)
REM usage:  restart-dpi.bat            -> DPI off (default)
REM         restart-dpi.bat system     -> SYSTEM_AWARE  (try this for sharpness)
REM         restart-dpi.bat pm2        -> PER_MONITOR_AWARE_V2
REM
REM Kill step: read the pid file the agent writes (%LOCALAPPDATA%\batbar\agent.pid)
REM and taskkill it. The old Get-CimInstance Win32_Process scan cost 3~8s and was
REM the reason the tray icon seemed to take 5~6s to appear.
setlocal
cd /d "D:\workbuddy\space\batbar"

set "PIDFILE=%LOCALAPPDATA%\batbar\agent.pid"
set PID=
if exist "%PIDFILE%" set /p PID=<"%PIDFILE%"

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
goto launch

:fallback
echo   (no live pid file - scan by image path)
powershell -NoProfile -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*batbar*' } | ForEach-Object { Write-Host ('  kill PID ' + $_.Id); Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }"

:launch
echo [t] kill step done    %TIME%
echo [2/2] Starting ONE fresh agent with BATBAR_DPI=%1 ...
set BATBAR_DPI=%1
start "" ".venv\Scripts\python.exe" -m batbar.agent --standalone
echo [t] agent launched    %TIME%











echo.
echo Done. Icon should appear within ~1s - see "boot: icon registered" in the log.
echo.
pause
endlocal






