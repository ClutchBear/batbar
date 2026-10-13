@echo off
rem one-shot setup for the Windows-side agent (run on Windows, not WSL).
rem NOTE: keep this file ASCII-only - cmd reads .cmd as GBK and UTF-8 Chinese
rem comments get mis-decoded (a trailing CJK byte can swallow the newline).
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if %errorlevel%==0 (
  echo [setup] creating venv with uv and installing agent deps ...
  uv venv .venv
  ".venv\Scripts\python.exe" -m pip install -e ".[agent]"
) else (
  echo [setup] uv not found, falling back to python -m venv ...
  py -3 -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -e ".[agent]"
)

echo [setup] firewall rule needs admin: run scripts\firewall-agent.ps1 separately
echo [setup] autostart: run scripts\register_autostart.ps1
echo [setup] done. start the agent with: run-agent.cmd
pause
