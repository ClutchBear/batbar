"""最后已知电量（last-known）持久化。

动机：蓝牙设备的电量并非每次都读得到。

  * 耳机走 HFP AG 上报电量，Windows 的 DEVPKEY_Device_BatteryLevel 在
    连上那一刻刷新一次，之后会回落成 NULL——实测西圣 XISEM-Air 第一次全量
    扫描读到 70%，紧接着三次查询都是 NULL。若「读不到就当设备不存在」，
    设备便会在菜单里闪烁消失。
  * 三模键盘切到 2.4G 接收器后，蓝牙链路上不再有任何电量源（见
    receiver24.py），此时唯一可用的就是它上一次在蓝牙上留下的读数。

所以这里存一张 {设备名: (电量, unix 时间戳)} 的表，跨进程、跨重启复用。
存盘路径由 `paths.py` 统一决定：**打包后 = exe 同目录下的 `last-known.json`**
（绿色模式），源码运行 = `%LOCALAPPDATA%\\batbar\\last-known.json`；任何 I/O 失败都静默降级
（只丢 memory，不影响主流程）。测试可用 BATBAR_LASTKNOWN 重定向到临时目录。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("batbar.lastknown")

_ENV = "BATBAR_LASTKNOWN"
_DIRNAME = "batbar"
_FILENAME = "last-known.json"

_mem: dict[str, tuple[int, float]] | None = None   # 内存缓存（懒加载）


def path() -> Path:
    """存盘文件位置。`BATBAR_LASTKNOWN` 可重定向到指定目录（测试用）。"""
    override = os.environ.get(_ENV)
    if override:
        return Path(override) / _FILENAME
    from . import paths

    return paths.lastknown_path()


def _load() -> dict[str, tuple[int, float]]:
    global _mem
    if _mem is not None:
        return _mem
    p = path()
    data: dict[str, tuple[int, float]] = {}
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            for name, v in (raw or {}).items():
                if isinstance(v, dict) and v.get("pct") is not None:
                    data[str(name)] = (int(v["pct"]), float(v.get("ts") or 0.0))
                elif isinstance(v, list) and len(v) == 2:
                    data[str(name)] = (int(v[0]), float(v[1]))
    except Exception as exc:  # noqa: BLE001 - 存档损坏就当没有，重建即可
        log.debug("last-known load failed: %s", exc)
    _mem = data
    return _mem


def _save() -> None:
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        obj = {n: {"pct": v[0], "ts": v[1]} for n, v in (_mem or {}).items()}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)          # 原子替换，避免写一半被断电
    except Exception as exc:    # noqa: BLE001
        log.debug("last-known save failed: %s", exc)


def put(name: str, percent: int) -> None:
    """记下某设备的最新读数。"""
    if percent is None:
        return
    table = _load()
    table[name] = (int(percent), time.time())
    _save()


def get(name: str) -> tuple[int, float] | None:
    """(电量, unix 时间戳)；没记录返回 None。"""
    try:
        return _load().get(name)
    except Exception:  # noqa: BLE001
        return None


def age(name: str) -> float | None:
    """记录距现在多少秒；无记录返回 None。"""
    got = get(name)
    return None if got is None else max(0.0, time.time() - got[1])


def all_records() -> dict[str, tuple[int, float]]:
    try:
        return dict(_load())
    except Exception:  # noqa: BLE001
        return {}


def reset() -> None:
    """清空（仅测试用）。"""
    global _mem
    _mem = {}
