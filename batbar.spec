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

5. Pillow 的 AVIF / WebP / CMS 插件一律剔除（v0.1.7）。
   `_avif` 单体 7.53 MB，比 batbar 自己的全部代码还大；我们只画 PNG 到托盘，
   这三个插件一行都用不到。
   ⚠️ 光写 excludes **挡不住**：PyInstaller 自带的 `hook-PIL.py` 里有一句
   `collect_dynamic_libs('PIL')`，会把 PIL 目录下**所有** .pyd 直接收进 binaries，
   根本不经过模块图。所以必须额外按**归档内的目标名**过滤 `a.binaries`。

6. `ssl` 有**两条**引入路径，两条都得堵 —— 只堵一条没用（v0.1.7 实测）。

   路径甲（已在源码侧断掉）：`applog.py` 的 `import logging.handlers`
       → CPython `logging/handlers.py` 顶层 import socket、函数体 import
         smtplib/http.client/ssl → `ssl`。
       现已改成自写的 `RotatingFileHandler`，这条链没了。

   路径乙（只能在这里挡）：`PIL.Image` 用了 `xml.etree.ElementTree`
       → PyInstaller 自带 `hook-xml.py` 里一句 `collect_submodules('xml')`
         把 **xml 整个包**（含 `xml.sax.*`）全收进来
       → `xml.sax.saxutils` 里 `import urllib`（取 URL 转义）
       → `urllib.request` → `http.cookiejar` / `ftplib` / `http.client` → `ssl`。

   ⚠️ 乙不能靠改 PIL 或改 hook 解决，只能 exclude。而 exclude `xml.sax.*` 是
   **安全**的（不像 v0.1.7 早期试过的「只 exclude ssl」会留半残模块图）：
   反查依赖图确认 **`xml.sax.*` 没有任何外部 import 者**，只有它自己人互相
   import；把整棵子树摘掉，留下的 `xml.etree.*`（PIL 真正要用的）完好无损。
   同理 `urllib.request` / `http.client` / `ftplib` 也整组一起摘，不留悬空引用。

   `tests/test_no_heavy_imports.py` 守住路径甲不回流；路径乙由本文件守，
   打包后若 `libcrypto-3-x64.dll` 又出现，多半是有人动了这里的 excludes。

排障提示：console=False 之后 stderr 无处可去，出问题看日志
    %LOCALAPPDATA%\\batbar\\logs\\agent.log
"""
import os

ROOT = os.path.abspath(SPECPATH)
ENTRY = os.path.join(ROOT, "scripts", "entry-exe.py")
VERSION = os.path.join(ROOT, "scripts", "version_info.txt")

#: 按**归档内目标名**（小写）剔除的二进制 —— 见文件头第 5 条。
#: 只删「确定没人事后加载」的：AVIF/WebP/CMS 三个 Pillow 插件。
DROP_BIN = {
    "_avif.cp312-win_amd64.pyd",
    "_webp.cp312-win_amd64.pyd",
    "_imagingcms.cp312-win_amd64.pyd",
}


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
        # ---- 见文件头第 6 条：掐断 ssl 的「路径乙」----
        # 整棵 xml.sax 子树（含 saxutils，它是 urllib 的入口）一起摘，
        # 不留悬空引用；xml.etree.* 保留，PIL 要用。
        "xml.sax", "xml.sax._exceptions", "xml.sax.expatreader",
        "xml.sax.handler", "xml.sax.saxutils", "xml.sax.xmlreader",
        "urllib.request", "http.cookiejar", "http.client", "ftplib",
        # _hashlib 是 libcrypto 的**最后一个**入口：它静态链接
        # libcrypto-3-x64.dll，留着它就等于留着 7.49 MB 的 OpenSSL。
        # 排除它是安全的（hashlib 降级到内置 _md5/_sha1/_sha2/_sha3/_blake2，
        # 见 tests/test_no_heavy_imports.py 的 HashlibWithoutOpenSSL）。
        "_hashlib",
    ],
    noarchive=False,
    optimize=0,
)

# 过滤必须在 Analysis 之后、PYZ 之前：hook-PIL 的 collect_dynamic_libs 已经把
# 插件 .pyd 塞进 a.binaries 了（见文件头第 5 条）。
a.binaries = [b for b in a.binaries
              if os.path.basename(str(b[0])).lower() not in DROP_BIN]

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
