"""生成 README 用的托盘图标预览图（用项目自己的 icons.py 渲染，真实不虚构）。

用法（在仓库根目录）：
    .venv\\Scripts\\python.exe tools\\render_icon_preview.py

产出（覆盖写）：
    assets/tray-icons-dark.png    深色任务栏底色
    assets/tray-icons-light.png   浅色任务栏底色
    assets/tray-icons-warning.png 低电量警告红

每张图 = 一排「真实 16px 显示」+ 下方一行对应放大图，外加电量刻度标注。
⚠️ 图标改版后请重跑本脚本刷新 README 配图，别手改 PNG。
"""
from __future__ import annotations

import io
import os
import sys

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from batbar.icons import IconSpec, render_icon  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
CANVAS = 32          # 图标画布（同运行时）
SHOW = 16            # 托盘真实显示尺寸
ZOOM = 8             # 放大倍数
CELL_PAD = 10

# (percent, warning, 刻度说明)
CASES = [
    (90, False, "90%"),
    (65, False, "65%"),
    (40, False, "40%"),
    (15, False, "15%"),
    (15, True, "15% 低电警告"),
    (None, False, "无设备/启动中"),
]

DARK_BG = (32, 32, 32)      # 深色任务栏
LIGHT_BG = (243, 243, 243)  # 浅色任务栏


def _cjk_font(size: int = 14):
    """中文标注字体（微软雅黑；找不到退回 PIL 默认）。"""
    for p in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def icon(percent, warning, light):
    return Image.open(io.BytesIO(render_icon(IconSpec(
        percent, False, CANVAS, light_taskbar=light, warning=warning,
    )))).convert("RGBA")


def strip(bg: tuple[int, int, int], light: bool, warning_all: bool,
          path: str, label_h: int = 22, cases=None) -> None:
    cases = cases or CASES
    cell_w = SHOW + CELL_PAD * 2
    zoom_w = ZOOM * SHOW
    W = len(cases) * max(cell_w, zoom_w + CELL_PAD)
    H = label_h + SHOW + 6 + zoom_w + label_h + 8
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    fg = (30, 30, 30) if light else (225, 225, 225)
    font = _cjk_font()

    for i, (pct, warn, caption) in enumerate(cases):
        w = warning_all or warn
        ic = icon(pct, w, light)
        s16 = ic.resize((SHOW, SHOW), Image.LANCZOS)
        x = i * max(cell_w, zoom_w + CELL_PAD)

        # 放大图（主体，居中）
        big = s16.resize((zoom_w, zoom_w), Image.NEAREST)
        im.paste(big, (x + (max(cell_w, zoom_w + CELL_PAD) - zoom_w) // 2,
                       label_h + SHOW + 6), big)
        # 真实 16px（贴在放大图上方居中）
        im.paste(s16, (x + (max(cell_w, zoom_w + CELL_PAD) - SHOW) // 2, label_h), s16)
        # 刻度说明
        tw = d.textlength(caption, font=font)
        d.text((x + (max(cell_w, zoom_w + CELL_PAD) - tw) / 2,
                label_h + SHOW + 6 + zoom_w + 4), caption, fill=fg, font=font)

    os.makedirs(OUT_DIR, exist_ok=True)
    im.save(path)
    print(f"-> {path}  {im.size}")


def main() -> None:
    strip(DARK_BG, False, False, os.path.join(OUT_DIR, "tray-icons-dark.png"))
    strip(LIGHT_BG, True, False, os.path.join(OUT_DIR, "tray-icons-light.png"))
    strip(DARK_BG, False, True, os.path.join(OUT_DIR, "tray-icons-warning.png"),
          cases=[(15, True, "15% 低电警告"), (8, True, "8% 低电警告"),
                 (None, False, "无设备/启动中")])


if __name__ == "__main__":
    main()
