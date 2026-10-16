"""singleinstance.py 单测：跨进程验证互斥量真的能挡住第二个实例。

── 为什么要用子进程 ──
命名互斥量的语义就是**跨进程**的。同一进程里第二次 CreateMutexW 会直接
成功（同一个持有者），那样测不出任何东西 —— 所以这里真起一个子进程当
「已存在的实例」，再让本进程去抢。

这正是踩过的坑的形状：托盘 agent 常驻后台占着 HID 队列，探针脚本再来抢，
表现为「收不到帧」而不是「打不开设备」，极难定位。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

from batbar import singleinstance

SRC = str(Path(singleinstance.__file__).resolve().parent.parent)

WIN = sys.platform == "win32"


def _unique() -> str:
    """每次测试一个独一无二的互斥量名，互不干扰、也不碰真实实例。"""
    return rf"Local\batbar-unittest-{uuid.uuid4().hex}"


def _spawn_holder(name: str, seconds: float = 5.0) -> subprocess.Popen:
    """起一个子进程占住 name，等它确认抢到再返回。"""
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from batbar import singleinstance as S\n"
        f"print('ok' if S.acquire({name!r}, notify=False) else 'busy', flush=True)\n"
        f"time.sleep({seconds})\n"
    )
    p = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    line = (p.stdout.readline() or "").strip()
    if line != "ok":
        err = p.stderr.read() if p.stderr else ""
        p.kill()
        raise AssertionError(f"holder cannot acquire: {line!r} {err}")
    return p


def _stop(p: subprocess.Popen) -> None:
    """结束持有者进程并收好 pipe（否则 ResourceWarning: unclosed file）。"""
    try:
        p.kill()
        p.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass
    finally:
        for s in (p.stdout, p.stderr):
            try:
                if s:
                    s.close()
            except Exception:  # noqa: BLE001
                pass


@unittest.skipUnless(WIN, "named mutex is Windows-only")
class CrossProcess(unittest.TestCase):
    def tearDown(self):
        singleinstance.release()

    def test_first_instance_acquires(self):
        self.assertTrue(singleinstance.acquire(_unique(), notify=False))

    def test_second_instance_blocked(self):
        name = _unique()
        holder = _spawn_holder(name)
        try:
            # 顺带断言它留了日志：静默启动时日志是唯一线索
            with self.assertLogs("batbar.single", level="ERROR"):
                self.assertFalse(singleinstance.acquire(name, notify=False))
        finally:
            _stop(holder)

    def test_lock_freed_after_holder_exits(self):
        name = _unique()
        holder = _spawn_holder(name, seconds=0.2)
        _stop(holder)
        # 持有者进程没了，内核会自动释放互斥量
        self.assertTrue(singleinstance.acquire(name, notify=False))

    def test_release_then_reacquire_same_process(self):
        name = _unique()
        self.assertTrue(singleinstance.acquire(name, notify=False))
        singleinstance.release()
        self.assertTrue(singleinstance.acquire(name, notify=False))


class Portable(unittest.TestCase):
    """非 Windows 上必须放行（不能因为平台不支持就让托盘起不来）。"""

    def test_non_windows_passthrough(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertTrue(singleinstance.acquire(_unique(), notify=False))

    def test_already_exists_constant(self):
        self.assertEqual(singleinstance.ERROR_ALREADY_EXISTS, 183)

    def test_release_when_not_held(self):
        singleinstance.release()  # 不能抛
