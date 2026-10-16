"""启动计时探针：测量 agent 启动到首帧图标各步骤耗时（不建托盘、不碰硬件）。

用法：.venv/Scripts/python.exe probes/probe_boot_timing.py
"""

import sys
import time

T0 = time.perf_counter()
MARKS: list[tuple[str, float]] = []


def mark(name: str) -> None:
    t = time.perf_counter()
    MARKS.append((name, t))
    print(f"[mark] {name:42s} +{t - T0:7.3f}s")


def main() -> int:
    mark("script start")

    import PIL

    mark(f"import PIL ({PIL.__version__})")

    from PIL import Image, ImageDraw, ImageFont

    mark("import PIL.Image/ImageDraw/ImageFont")

    import pystray

    mark("import pystray")

    from batbar import icons as I

    mark("import batbar.icons")

    from batbar import agent as A

    mark("import batbar.agent (heavy: hidpp/transport/ctypes)")

    # 字体加载：icon 渲染第一枪的真实成本
    t = time.perf_counter()
    spec_t = time.perf_counter()
    try:
        f = ImageFont.load_default()
        print(f"  load_default: {time.perf_counter() - spec_t:.3f}s")
    except Exception as exc:
        print("  load_default failed:", exc)
    mark("PIL font load_default")

    # 首帧渲染（含字体/度量初始化）
    spec = I.IconSpec(None, False, 32, light_taskbar=False, no_devices=True)
    png1 = I.render_icon(spec)
    mark(f"render_icon #1 (-- / {len(png1)}B)")

    png2 = I.render_icon(I.IconSpec(90, False, 32, light_taskbar=False))
    mark(f"render_icon #2 (90% / {len(png2)}B)")

    png3 = I.render_icon(I.IconSpec(None, False, 32, light_taskbar=False))
    mark(f"render_icon #3 (? / {len(png3)}B)")

    # 系统深色探测（可能读注册表）
    try:
        dark = A._detect_sys_dark()
    except Exception as exc:
        dark = f"ERR {exc}"
    mark(f"_detect_sys_dark -> {dark}")

    # Agent 构造（不读硬件）
    a = A.Agent(None)
    mark("Agent.__init__")

    # _paint_boot 在无托盘时应当走「没有 icon → False」快路径
    ok = a._paint_boot()
    mark(f"_paint_boot (no tray) -> {ok}")

    print("\n[summary] 相对 script start 的累计耗时：")
    prev = T0
    for name, t in MARKS:
        print(f"  {name:42s} {t - T0:7.3f}s  (+{t - prev:6.3f}s)")
        prev = t
    return 0


if __name__ == "__main__":
    sys.exit(main())
