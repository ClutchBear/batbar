"""生成 README 用的托盘图标预览图（用项目自己的 icons.py 渲染，真实不虚构）。

用法（在仓库根目录）：
    .venv\\Scripts\\python.exe tools\\render_icon_preview.py

产出（覆盖写）：
    assets/tray-icons-dark.png    深色任务栏底色
    assets/tray-icons-light.png   浅色任务栏底色
    assets/tray-icons-warning.png 低电量警告红

清晰度策略（2026-10-15 信哥定稿：上一版「16px 放大 8 倍」又糊又块，废弃）：
  - 大图 = **原生大画布渲染**（icons.py 本身按 spec.size 等比缩放，size=128
    直接得到清晰边缘，零缩放损耗）
  - 小图 = 32px 画布 LANCZOS 降到 16px 后 **1:1 原大贴图**（托盘里的真实样子，
    无任何放大，自然不糊）
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
CANVAS = 32     # 图标画布（同运行时）
SHOW = 16       # 托盘真实显示尺寸（1:1 原大展示）
BIG = 128       # 大图原生渲染画布（不是放大，是直接画这么大）
CELL_PAD = 14
ROW_GAP = 8

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


def icon(percent, warning, light, size=CANVAS):
    return Image.open(io.BytesIO(render_icon(IconSpec(
        percent, False, size, light_taskbar=light, warning=warning,
    )))).convert("RGBA")


def strip(bg: tuple[int, int, int], light: bool, warning_all: bool,
          path: str, cases=None) -> None:
    cases = cases or CASES
    cell = BIG + CELL_PAD * 2
    W = len(cases) * cell
    label_h = 20
    H = SHOW + ROW_GAP + BIG + label_h + 6
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    fg = (60, 60, 60) if light else (170, 170, 170)
    font = _cjk_font()

    for i, (pct, warn, caption) in enumerate(cases):
        w = warning_all or warn
        x0 = i * cell
        # 上排：托盘真实大小（32→16 LANCZOS，1:1 贴图，无放大）
        s16 = icon(pct, w, light).resize((SHOW, SHOW), Image.LANCZOS)
        im.paste(s16, (x0 + (cell - SHOW) // 2, 0), s16)
        # 主图：原生大画布渲染（清晰边缘，零缩放）
        big = icon(pct, w, light, size=BIG)
        im.paste(big, (x0 + CELL_PAD, SHOW + ROW_GAP), big)
        # 刻度说明
        tw = d.textlength(caption, font=font)
        d.text((x0 + (cell - tw) / 2, SHOW + ROW_GAP + BIG + 4),
               caption, fill=fg, font=font)

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
