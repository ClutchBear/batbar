"""守住「句柄必须全部关闭」+「一个 class body 里不许有重名方法」。

起因
----
2026-10-16 代码审查实锤的 bug：`transport.py` 里 `HidapiTransport.close()`
**被定义了两次**（原 128 / 136 行）。Python 后定义胜出，生效的那份只关
`self._dev`，于是 `self._readers` 里的**兄弟 collection 句柄一个都没关** ——
每次 `agent._drop_device()`（设备判离线/重枚举）都漏一批 Windows HID 句柄，
反复拔插会一直累积。

★ 为什么它能活到今天
--------------------
518 个用例里没有一条碰过 `HidapiTransport.close`：`tests/test_mouse_probe.py`
把整个类 patch 掉了（那里要测的是槽位发现，不是句柄生命周期）。也就是说
**全覆盖的测试数骗人** —— 被 mock 掉的那条路径恰恰是唯一会漏资源的路径。

所以这里补两条互相独立的守卫：
  行为 —— 用**真类** + 假设备跑一遍，断言 close() 后 primary 和每一个 reader
          都是 closed（不 mock 被测类，否则守卫等于没写）；
  结构 —— AST 扫全源码，禁掉「同一个 class body 里出现重名方法」这一整类 bug
          （不只是 `close`：任何方法重名都是**静默覆盖**，没有语法错误、没有
          lint 报警，只有运行时行为悄悄变了）。

⚠️ 扫描型守卫最危险的失败模式是「永远通过」：路径写错、一个文件都没扫到 ⇒
空列表 ⇒ 断言直接过 ⇒ 比没有守卫更坏。所以下面带自证和阳性对照。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from batbar.transport import HidapiTransport

SRC = Path(__file__).resolve().parents[1] / "src" / "batbar"


class _FakeDev:
    """假 hid device：只记录自己被关了没、以及要吐出的输入帧。"""

    def __init__(self, name: str, frames: tuple[bytes, ...] = ()) -> None:
        self.name = name
        self.closed = False
        self._frames = list(frames)

    def close(self) -> None:
        self.closed = True

    def read(self, _n: int) -> list[int]:
        if not self._frames:
            return []
        return list(self._frames.pop(0))


class CloseClosesEveryHandle(unittest.TestCase):
    """close() 必须关掉 primary + 全部兄弟 collection（本 bug 的正主）。"""

    def _transport(self, n_siblings: int = 2) -> tuple[HidapiTransport, list[_FakeDev]]:
        dev = _FakeDev("primary")
        sibs = [_FakeDev(f"reader{i}") for i in range(1, n_siblings + 1)]
        return HidapiTransport(dev, frame_len=7, readers=sibs), [dev, *sibs]

    def test_close_closes_primary_and_all_siblings(self):
        tr, devs = self._transport()
        self.assertEqual(3, len(tr._readers))  # primary + 2 兄弟

        tr.close()

        leaked = [d.name for d in devs if not d.closed]
        self.assertEqual([], leaked, "这些句柄没被关闭（兄弟 collection 又漏了？）：" + ", ".join(leaked))
        self.assertTrue(tr._closed)

    def test_close_without_siblings_still_closes_primary(self):
        """只有 primary 时行为不变（退化路径别改坏）。"""
        tr, devs = self._transport(n_siblings=0)
        tr.close()
        self.assertEqual(["primary"], [d.name for d in devs])
        self.assertTrue(devs[0].closed)

    def test_close_tolerates_a_failing_handle(self):
        """某个 reader 关闭抛异常时，后面的仍要被关掉（不能整批放弃）。"""
        dev, bad, good = _FakeDev("primary"), _FakeDev("bad"), _FakeDev("good")

        def boom() -> None:
            raise OSError("device gone")

        bad.close = boom  # type: ignore[method-assign]
        tr = HidapiTransport(dev, frame_len=7, readers=[bad, good])

        tr.close()  # 不该抛
        self.assertTrue(dev.closed)
        self.assertTrue(good.closed, "前面那个 reader 抛异常，把后面的连坐了")

    def test_read_polls_every_reader(self):
        """读要能拿到**兄弟 collection** 上的帧（单读者读不到，正是要有 _readers 的原因）。"""
        dev = _FakeDev("primary")  # 永远空
        sib = _FakeDev("reader1", frames=(b"\x11\x01",))
        tr = HidapiTransport(dev, frame_len=7, readers=[sib])

        self.assertEqual(b"\x11\x01", tr.read(0.05))


class NoDuplicateMethods(unittest.TestCase):
    """结构守卫：同一个 class body 里不许出现重名方法。"""

    @staticmethod
    def _duplicates(source: str) -> list[str]:
        """返回 ['类名.方法名 @ 行号列表', ...]；只看**同一层** class body。"""
        out: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ClassDef):
                continue
            seen: dict[str, list[int]] = {}
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seen.setdefault(b.name, []).append(b.lineno)
            for name, lines in seen.items():
                if len(lines) > 1:
                    out.append(f"{node.name}.{name} @ {lines}")
        return out

    def test_scan_reaches_sources(self):
        """自证扫到了东西 —— 防空列表假通过。"""
        files = sorted(SRC.rglob("*.py"))
        self.assertGreater(len(files), 10, f"没扫到源码，SRC 错了？{SRC}")
        self.assertTrue((SRC / "transport.py").is_file())

    def test_no_duplicate_methods_in_src(self):
        hits: list[str] = []
        for f in sorted(SRC.rglob("*.py")):
            for dup in self._duplicates(f.read_text(encoding="utf-8")):
                hits.append(f"{f.relative_to(SRC)}: {dup}")
        self.assertEqual(
            [], hits, "同一 class body 里有重名方法（后定义的静默覆盖前定义）：\n" + "\n".join(hits)
        )

    def test_scanner_detects_the_original_bug(self):
        """阳性对照：把当年的 bug 原样喂进去，扫描必须报出来。"""
        sample = (
            "class HidapiTransport:\n"
            "    def close(self) -> None:\n"
            "        for r in self._readers:\n"
            "            r.close()\n"
            "\n"
            "    def close(self) -> None:\n"
            "        self._dev.close()\n"
        )
        dups = self._duplicates(sample)
        self.assertEqual(1, len(dups), f"没抓到重复定义：{dups}")
        self.assertIn("HidapiTransport.close", dups[0])

    def test_scanner_allows_same_name_in_different_classes(self):
        """阴性对照：不同类里有同名方法是正常的（多态/协议实现），不该报。"""
        sample = "class A:\n    def close(self) -> None: ...\nclass B:\n    def close(self) -> None: ...\n"
        self.assertEqual([], self._duplicates(sample))


if __name__ == "__main__":
    unittest.main()
