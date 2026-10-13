# -*- mode: python ; coding: utf-8 -*-
"""batbar 单文件 exe 的 PyInstaller 规格（onefile + 无控制台）。

构建（在项目根执行）：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean batbar.spec
或直接双击 build-exe.bat。

产物：dist\\batbar.exe

为什么是这个写法 —— 四条都对应踩过的坑，改动前请先读完：

1. `pystray._win32` 必须是 hiddenimport。
   pystray 的托盘后端是**运行时按平台动态 import** 的（`_win32` / `_darwin`
   / `_gtk` / `_appindicator`），静态分析扫不到它；不写就在启动瞬间
   `ImportError: No module named 'pystray._win32'` —— 而且因为是 windowed
   无控制台，你只会看到"双击没反应"。

2. `hid` 显式写出。
   cython-hidapi 的产物是**顶层扩展模块** `hid.cp312-win_amd64.pyd`，直接躺在
   site-packages 根下而不是 hid/ 包目录里，PyInstaller 对这类"平台后缀顶层
   模块"偶尔会漏。显式写比赌它猜中便宜。

3. `tkinter` 必须排除。
   Pillow 的官方 hook 会把 `ImageTk` 拉进来，连带 tcl/tk 运行时（约 12 MB）；
   本项目只画 PNG 到托盘，一行 tkinter 都不用。

4. onefile 形态 = EXE(...) 直接吃 a.binaries / a.datas，**不要**再套 COLLECT。
   套了会变成 onedir，产物从 1 个 exe 变成一整个文件夹。

排障提示：console=False 之后 stderr 无处可去，出问题看日志
    %LOCALAPPDATA%\\batbar\\logs\\agent.log
"""
import os

ROOT = os.path.abspath(SPECPATH)
ENTRY = os.path.join(ROOT, "scripts", "entry-exe.py")
VERSION = os.path.join(ROOT, "scripts", "version_info.txt")


a = Analysis(
    [ENTRY],
    # src 显式入 path：不依赖 .venv 里那个 editable 安装的 .pth（它写死绝对路径，
    # 目录一改名/一搬到别的机器就失效）
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=[],
    hiddenimports=["pystray._win32", "hid", "PIL._imaging"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest", "_pytest", "pytest_timeout",
        "setuptools", "numpy", "matplotlib",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="batbar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # 没装 UPX；装了也建议不开（会触发杀软误报）
    runtime_tmpdir=None,
    console=False,                   # 托盘程序不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "assets", "batbar.ico"),
    version=VERSION,
)
