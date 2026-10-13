@echo off
chcp 65001 >nul
REM batbar 一键打包成单文件 exe（PyInstaller）。
REM 双击即可；产物 dist\batbar.exe
setlocal
cd /d "%~dp0"

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [X] 找不到 .venv\Scripts\python.exe
  echo     先在项目根执行：uv sync --extra agent
  exit /b 1
)

"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
  echo [X] 当前 venv 没装 PyInstaller，先装：
  echo     uv pip install --python "%PY%" pyinstaller
  exit /b 1
)

if not exist "assets\batbar.ico" (
  echo [*] 生成图标 assets\batbar.ico ...
  "%PY%" tools\make_icon.py || exit /b 1
)

echo [*] 正在打包（首次约 40~90 秒，之后走缓存更快）...
"%PY%" -m PyInstaller --noconfirm --clean batbar.spec
if errorlevel 1 (
  echo.
  echo [X] 打包失败 —— 看上面的报错。
  exit /b 1
)

echo.
echo [OK] 产物：%CD%\dist\batbar.exe
for %%F in ("dist\batbar.exe") do echo      体积：%%~zF 字节
echo.
echo   双击即运行（无控制台窗口，图标在通知区域）。
echo   排障日志：%LOCALAPPDATA%\batbar\logs\agent.log
echo   注意：托盘程序没有窗口，退出请右键托盘图标 ^-^ 退出。
endlocal
