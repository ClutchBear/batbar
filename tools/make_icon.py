"""生成 assets/batbar.ico —— exe 图标（多尺寸，逐尺寸独立渲染）。

为什么不复用 icons.py：那里是**托盘**图标（16px 单色数字，用户定稿）；
本文件是**资源管理器/任务栏缩略**用的 .ico（最大 256px），那个尺寸下
电池造型看得清，画成图形比数字更像个"应用"。

配色（浅色/深色背景都要清晰，故外壳走中性深灰、电量条走亮绿）：
  外壳填充 #2B2F36  /  描边 #4A5158  /  电量条 #22C55E
"""
from __future__ import annotations

import io
import os
import struct

from PIL import Image, ImageDraw

SHELL_FILL = (43, 47, 54, 255)      # #2B2F36
SHELL_LINE = (74, 81, 88, 255)      # #4A5158
BAR_FILL = (34, 197, 94, 255)       # #22C55E

SIZES = [16, 24, 32, 48, 64, 128, 256]
LEVEL = 0.70                        # 电量条占可用宽度的比例


def draw_battery(size: int) -> Image.Image:
    """按相对坐标绘制 —— 每个尺寸独立渲染，小尺寸才不糊。"""
    # 4x 超采样后缩回，边缘更干净（小尺寸尤其明显）
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    line = max(ss, round(s * 0.042))          # 描边宽
    radius = max(ss, round(s * 0.085))        # 主体圆角

    # 电池帽（右侧小凸起），先画，让主体压住它的左边缘
    cap_l = round(s * 0.795)
    cap_r = round(s * 0.905)
    cap_t = round(s * 0.415)
    cap_b = round(s * 0.585)
    d.rounded_rectangle([cap_l, cap_t, cap_r, cap_b],
                        radius=max(1, round(s * 0.028)), fill=SHELL_LINE)

    # 主体
    body = [round(s * 0.085), round(s * 0.265), round(s * 0.815), round(s * 0.735)]
    d.rounded_rectangle(body, radius=radius, fill=SHELL_FILL,
                        outline=SHELL_LINE, width=line)

    # 电量条（内缩一个描边 + 一点呼吸）
    ins = round(s * 0.075)
    bx0 = body[0] + ins
    by0 = body[1] + ins
    bx1 = body[2] - ins
    by1 = body[3] - ins
    filled_r = bx0 + round((bx1 - bx0) * LEVEL)
    d.rounded_rectangle([bx0, by0, filled_r, by1],
                        radius=max(1, round(s * 0.035)), fill=BAR_FILL)

    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: str, frames: list[Image.Image]) -> None:
    """手写 ICO 容器：每档一帧 PNG（Vista+ 支持 PNG-in-ICO）。

    不用 Pillow 的 ICO 保存 —— 它只对**单张主图**做 resize，会丢掉我们逐档
    渲染的结果；16px 上手工调过的描边宽度被缩一次就退化成糊边。
    """
    blobs = []
    for im in frames:
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        blobs.append(buf.getvalue())

    n = len(blobs)
    entries = b""
    offset = 6 + 16 * n
    for im, blob in zip(frames, blobs):
        # ICO 里 256 用 0 表示（尺寸字段只有 1 字节）
        w = 0 if im.width >= 256 else im.width
        h = 0 if im.height >= 256 else im.height
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)

    with open(path, "wb") as fp:
        fp.write(struct.pack("<HHH", 0, 1, n) + entries + b"".join(blobs))


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(root)                       # _tmp/ 的父目录 = 项目根
    out_dir = os.path.join(root, "assets")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "batbar.ico")

    frames = [draw_battery(n) for n in SIZES]
    frames.sort(key=lambda im: im.width)
    write_ico(out, frames)

    print(f"写入 {out}  ({os.path.getsize(out):,} B)")
    # 复核：读回来确认条目数与每档都能解码
    back = Image.open(out)
    print("  ICO 内条目:", getattr(back, "ico", None) and back.ico.sizes())
    for n in SIZES:
        back.size = (n, n)          # Pillow 用这个方式切帧
        im = back.copy()
        assert im.size == (n, n), (n, im.size)
    print("  逐档解码 OK:", SIZES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
