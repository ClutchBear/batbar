"""feature 样本采集器 —— 给迈从 K87 的电量语义定案用。

背景：05AC:024F 的 0xFF04/0x0002 collection 有 feature report 0x06，
样本 `06 00 02 64 64 64 65 92`。64 64 64 65 是电芯电压（×50 → 3.20/3.25V）
还是百分比原始值？单样本无法定案 —— 需要在**不同电量**下多采几组：
若四个字节随电量线性下降且落在 60-65 区间 ⇒ 电压；若从 64 直接掉到
5x/4x 这种整数百分比形态 ⇒ 百分比。

用法（托盘可同时在跑：GetFeature 是同步 IOCTL，不抢输入队列）：
    python tools/feature_watch.py                 # 默认 60s 一拉，跑 2 小时
    python tools/feature_watch.py --interval 30 --duration 0   # 常驻（Ctrl+C 停）

样本落盘 _tmp/feature-samples-<日期>.md，攒够跨电量的样本后发我解译。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from batbar import feature24  # noqa: E402
from batbar.dongle import TARGETS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="feature report 样本采集")
    ap.add_argument("--interval", type=float, default=60.0, help="拉取间隔秒")
    ap.add_argument("--duration", type=float, default=7200.0, help="总时长秒（0 = 常驻）")
    ap.add_argument("--out", default="_tmp", help="样本目录")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logp = out / f"feature-samples-{datetime.now():%Y%m%d}.md"

    def w(s=""):
        line = f"[{time.strftime('%H:%M:%S')}] {s}"
        print(line, flush=True)
        with logp.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # 先对每个目标做一次发现（穷举 feature ID），报告命中情况
    for (vid, pid), name in TARGETS.items():
        hits = feature24.discover(name, vid, pid)
        w(
            f"发现 {name} {vid:04X}:{pid:04X}: "
            + (f"{len(hits)} 个可读 feature 口" if hits else "无 feature 口")
        )
        for path, featlen, rid in hits:
            data = feature24.read_feature(path, featlen, rid)
            w(f"  rid=0x{rid:02x} len={featlen}: {data.hex(' ') if data else '读失败'}")

    t_end = time.time() + args.duration if args.duration > 0 else None
    w(
        f"开始周期采集（间隔 {args.interval:.0f}s"
        + ("，常驻" if t_end is None else f"，至 {datetime.fromtimestamp(t_end):%H:%M}")
        + "）。样本落盘："
        + str(logp.resolve())
    )
    try:
        while True:
            for name in list(TARGETS.values()):
                got = feature24.poll_once(name)
                for data in got:
                    w(f"SAMPLE {name} {data.hex(' ')}")
            if t_end and time.time() >= t_end:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        w("中断退出，已采样本见文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
