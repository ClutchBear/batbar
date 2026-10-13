@echo off
rem start batbar agent (Windows, foreground, for debugging).
rem NOTE: keep this file ASCII-only - cmd reads .cmd as GBK, UTF-8 Chinese
rem comments get mis-decoded and splice into the next command line.
rem
rem Env vars (optional):
rem   set BATBAR_DEBUG=1            - DEBUG level log (default INFO)
rem   set BATBAR_LOG_DIR=D:\tmp\lg  - redirect log dir
rem                                 (default %LOCALAPPDATA%\batbar\logs)
rem   set BATBAR_24G_TIMEOUT=120    - 2.4G silence window in seconds (default 90)
rem A second instance is refused on purpose (two handles would fight over the
rem same HID input queue); use --allow-multiple only when debugging.
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m batbar.agent --standalone -v
) else if exist ".venv-win\Scripts\python.exe" (
  ".venv-win\Scripts\python.exe" -m batbar.agent --standalone -v
) else (
  echo [agent] no venv found - run setup-agent.cmd first
  pause
)
