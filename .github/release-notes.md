## 下载

**`batbar-*-win64.zip`** —— 解压即用，**不需要装 Python**，不需要任何运行时。

1. 解压到任意位置（例如 `D:\batbar\`）
2. 双击 `batbar.exe` —— 它会常驻系统托盘（**没有窗口，属正常**）
3. 退出：托盘图标右键 → 退出

压缩包里还附了 `使用说明.txt`，含配置项与故障排查。

## 系统要求

Windows 10 / 11 64 位。

## 这个包是怎么来的

由 GitHub Actions 从本 tag 的源码在 `windows-latest` 上自动构建：

```
python -m pip install -e ".[agent,dev]" pyinstaller
python -m pytest
python -m PyInstaller --noconfirm --clean batbar.spec
```

`dist\batbar.exe` + `LICENSE` + `使用说明.txt` 打包成 zip。构建步骤见仓库的
[`.github/workflows/build-exe.yml`](https://github.com/ClutchBear/batbar/blob/main/.github/workflows/build-exe.yml)，
本地打包用 `build-exe.bat`，两条路径的产物一致。

## 许可与归属

MIT，Copyright (c) 2026 ClutchBear。

第三方依赖：cython-hidapi（BSD-3-Clause）、pystray（LGPL-3.0）、Pillow（MIT-CMU）。
