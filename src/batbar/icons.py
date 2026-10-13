"""电量图标渲染（Pillow，纯函数，输入百分比 → 输出 PNG bytes）。

设计：任务栏托盘实际显示 16px，电池造型在这个尺寸下看不清轮廓，
故直接画**百分比数字**（用户要求）。

配色 = 与任务栏系统图标（音量/WiFi/网络）同款单色（用户定稿 2026-10-12）：
  浅色任务栏 → 黑数字；深色任务栏 → 白数字。
  不做描边（系统图标都没有）。
  充电态**不画 ⚡**（用户定稿 2026-10-12 22:23：托盘图标上不要充电符号，
  LEOBOG/K87 都不）；充电信息只体现在菜单文字与 tooltip。

清晰度策略：
  - 画布 32×32 渲染（数字能放到最大、hinting 最充分），Pillow ICO 编码器
    自动降采样出 16/24 条目（LANCZOS），比直接在 16px 上画字清晰得多
  - 字体优先 Segoe UI Bold（Windows 自带、小字号可读性好）
  - 三位数（100）在 16px 下放不下 → 显示 "99"（电量显示惯例）
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

# 单色：与 Win11 任务栏系统图标同款——浅底近黑、深底近白
COLOR_ON_LIGHTTASKBAR = (14, 14, 14, 255)
COLOR_ON_DARKTASKBAR = (244, 244, 244, 255)

# 低电量警告红：取自用户截图（Windows Terminal 错误红调色板，用户定稿 2026-10-12）
#   #c50f1f = 截图主红（白底上够沉够艳）；#e74856 = 同族亮阶（暗底上更跳，
#   深色任务栏用 #c50f1f 对比度仅 2.7:1 偏暗，换亮阶 4.2:1）
RED_ON_DARKTASKBAR = (231, 72, 86, 255)      # #e74856
RED_ON_LIGHTTASKBAR = (197, 15, 31, 255)     # #c50f1f

# 候选字体（Windows 路径 + 通用名兜底）
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf",   # Segoe UI Bold
    "C:/Windows/Fonts/arialbd.ttf",    # Arial Bold
    "arialbd.ttf",
    "segoeuib.ttf",
]
_font_cache: dict[int, object] = {}


def _font(size: int):
    """按像素大小取粗体字体，带缓存；全失败退回 PIL 默认位图字体。"""
    if size in _font_cache:
        return _font_cache[size]
    f = None
    for path in _FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if f is None:
        f = ImageFont.load_default()
    _font_cache[size] = f
    return f


def color_for(percent: int | None, light_taskbar: bool = False) -> tuple[int, int, int, int]:
    """单色配色（percent 参数保留仅为向后兼容签名）：按任务栏深浅取黑/白。"""
    return COLOR_ON_LIGHTTASKBAR if light_taskbar else COLOR_ON_DARKTASKBAR


@dataclass
class IconSpec:
    percent: int | None
    charging: bool
    size: int
    light_taskbar: bool = False   # True=浅色任务栏（黑字）；False=深色（白字）
    warning: bool = False         # True=低电量警告（数字变红；percent=低电设备读数）
    no_devices: bool = False      # True=一台设备都不在（语义标记：tooltip 用
                                  # 「未发现设备」）。⚠️ 2026-10-13 起**不再影响
                                  # 像素**——percent=None 一律画 "--"，用户定稿
                                  # 「任何时候都不显示问号」。

    def key(self) -> tuple:
        return (None if self.percent is None else (self.percent // 5) * 5,
                self.charging, self.size, self.light_taskbar, self.warning,
                self.no_devices)


def _text_for(percent: int | None, charging: bool) -> str:
    if percent is None:
        # 2026-10-13 用户定稿：**任何时候都不显示 "?"**（"--" 观感更好，且
        # "?" 在"设备根本没连"时纯属误导）。语义差异改由 tooltip 承担：
        # 「启动中…」/「未发现设备」/「{设备名}：未知」。
        return "--"
    p = max(0, min(99, percent))          # 100 → 99：三位数在 16px 不可读
    return str(p)


def _draw_bolt(d: ImageDraw.ImageDraw, cx: float, cy: float, k: float,
               fill=COLOR_ON_DARKTASKBAR) -> None:
    """手绘闪电（U+26A1 在 Segoe UI Bold 无字形，Pillow 不做字体回退，
    故用折线多边形，任何字号都清晰）。cx,cy=中心，k=半高。

    ⚠️ 2026-10-12 22:23 起**不再被调用**：用户拍板托盘图标不画充电符号
    （LEOBOG/K87 都不要）。render_icon 恒按无充电布局画纯数字；函数保留
    备用（菜单文字与 tooltip 的 ⚡ 不受影响，那是文案不是图标）。
    """
    pts = [(cx + k * 0.30, cy - k), (cx - k * 0.55, cy + k * 0.15),
           (cx - k * 0.05, cy + k * 0.15), (cx - k * 0.30, cy + k),
           (cx + k * 0.55, cy - k * 0.15), (cx + k * 0.05, cy - k * 0.15)]
    d.polygon(pts, fill=fill)


def render_icon(spec: IconSpec) -> bytes:
    """绘制单个数字电量图标，返回 PNG bytes（画布 spec.size，推荐 32）。

    Args:
        spec: percent（**None=读不出 → 画 "--"**，2026-10-13 起不再画 "?"）、size、
              light_taskbar（True=浅色任务栏黑字 / False=深色任务栏白字）、
              warning（True=低电红字）。
              charging **只进 key/tooltip，不再进图标**（2026-10-12 用户
              拍板：托盘图标不画 ⚡，恒为纯数字居中）。
    Returns:
        PNG bytes，透明底
    """
    s = spec.size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 读不出电量一律画 "--"（2026-10-13 用户定稿：**任何时候都不显示 "?"**）。
    # `no_devices` 不再参与文本选择，只作为语义标记进 key()/tooltip 逻辑。
    text = _text_for(spec.percent, False)
    light = bool(getattr(spec, "light_taskbar", False))
    if getattr(spec, "warning", False):
        color = RED_ON_LIGHTTASKBAR if light else RED_ON_DARKTASKBAR
    else:
        color = color_for(spec.percent, light)

    # 纯数字居中（无闪电；充电态图标与常规态唯一区别由 tooltip/菜单体现）
    max_w = int(s * 0.88)
    max_h = int(s * 0.82)
    # 二分找最大可用字号
    lo, hi, best = 6, s, 6
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _font(mid)
        bb = d.textbbox((0, 0), text, font=f)
        if (bb[2] - bb[0]) <= max_w and (bb[3] - bb[1]) <= max_h:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    f = _font(best)

    # 视觉居中（锚点 "mm" 对齐可用区几何中心）；无描边，与系统图标同款
    cx = s // 2
    cy = s // 2 + 1
    d.text((cx, cy), text, font=f, fill=color, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
