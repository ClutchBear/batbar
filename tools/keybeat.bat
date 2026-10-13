@echo off
rem keybeat launcher - run from anywhere, always resolves paths relative to this .bat
chcp 65001 >nul
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 tools\keybeat.py %*
) else (
  py -3 -X utf8 tools\keybeat.py %*
)
echo.
echo (report saved under _tmp\)
pause
