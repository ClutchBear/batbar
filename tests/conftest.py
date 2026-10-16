"""pytest 全局 fixture。

这里的唯一职责：**把测试对磁盘的任何写入关进临时目录**。

背景（2026-10-14 真踩）：测试跑完以后，`%LOCALAPPDATA%\\batbar\\last-known.json`
里躺的是测试用例的假设备（耳机 QCY Era 7 / 键盘 LEOBOG Hi98C Pro）。本地跑只是
看着别扭；但 CI 的冒烟步骤会用 `--help` 真启动一次 exe，绿色模式下数据目录就是
exe 所在目录（= 发布目录），搬迁逻辑随即把这份**测试数据**复制进发布目录，最后
被打进 zip 随 Release 发出去 —— v0.1.2 首次构建的附件里就多了一个
`last-known.json`。

两处修法都做了（缺一不可）：
  * CI 侧：把泄漏检查挪到冒烟之后，并断言发布目录只允许三类文件；
  * 这里：**让测试从一开始就不写真实目录**。逐个测试补隔离是补不完的（走的是
    `bt_battery._accept()` → `lastknown.put()` 这种间接路径），所以在顶层一次性
    重定向 `lastknown` 的落点。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_lastknown(tmp_path, monkeypatch):
    """每个用例独占一张 last-known 存档表。

    `lastknown` 的 `_mem` 是**模块级缓存**（懒加载一次），所以光重定向环境变量
    不够 —— 前一个用例 `put` 进去的记录会留在内存里串给下一个用例，即使文件
    已经换地方了。前后各 `reset()` 一次才是真的隔离。
    """
    from batbar import lastknown

    monkeypatch.setenv(lastknown._ENV, str(tmp_path))
    lastknown.reset()
    yield
    lastknown.reset()
