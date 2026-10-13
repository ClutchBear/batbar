@echo off
rem smoke-test launcher - black-box smoke test for batbar agent
rem Double-click to run; always resolves paths relative to this .bat
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 tests\smoke_launch.py %*
) else (
  py -3 -X utf8 tests\smoke_launch.py %*
)
echo.
pause
