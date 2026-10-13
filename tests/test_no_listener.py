"""打包后的 exe 不得再开任何监听端口（2026-10-14 用户报的防火墙弹窗）。

起因：旧架构是「Windows 侧 agent + WSL2 侧 controller」双进程，靠本地 TCP 桥
（`0.0.0.0:51788`）通信。后来改成单进程自跑，controller 那条路早已废弃，但
`agent.py` 里 `HOST = "0.0.0.0"` / `PORT = 51788` 和 `_serve()` 没有一起删
⇒ 每次双击 exe，Windows 防火墙就弹一次「是否允许 batbar.exe 访问网络」。

⚠️ 分清因果：**弹窗的是 agent.py 那个监听**（v0.1.3 删）。`controller.py`
是 TCP **客户端**、`wire.py` 只是编解码 —— 它们从没监听过任何端口，只是
「那个监听存在的理由」。三件套在 v0.1.4 随桥接一起删除，别把守卫指错方向。

⚠️ 为什么必须靠测试守住，不能靠手测：

1. **防火墙提示**通常**每台机器只弹一次** —— 在本机点过「取消」之后就再也不
   弹了，于是「我这边没看到」是完全没有证明力的（当年黑窗 bug 也是这个德性：
   源码跑复现不出）。
2. 监听的是 `0.0.0.0`（**全部网卡**），不只是 127.0.0.1 ⇒ 等于对局域网开放
   一个没人用的端口。这既是弹窗来源，也是无谓的攻击面。

判定口径：不看「有没有 import socket」，只看**「有没有真的 listen」** ——
禁止 `.bind(` 与 `listen(` 这两个动作。旧双进程架构（agent + controller 走
本地 TCP 桥）已连根删掉，任何形态的监听都不该再出现。

⚠️ **扫描型守卫最容易犯的错是「永远通过」**：SRC 路径算错、或 glob 一个文件
都没匹配到 ⇒ 结果是空列表、断言直接过 ⇒ 比没有守卫更危险（给了虚假安全感）。
所以下面每条扫描断言都**先自证扫到了东西**（见 `test_scan_actually_reaches_sources`
与各断言里的存在性检查），再断言内容。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "batbar"

# 「开监听」的两个决定性动作：绑定地址 + 开始 listen。
# 用 `\.bind\(` 而非 `\bbind\(`：实参绑定的写法是 `s.bind(...)`；
# 顺带避开 `widget.bind(`（GUI 事件绑定，跟端口无关）这类同名无关用法。
_PATTERNS = {
    "bind": re.compile(r"\.bind\("),
    "listen": re.compile(r"\blisten\("),
}

# 旧桥接三件套 —— 可从 git 历史里捞回来，故需按**文件名**在任意层级守门
# （只查顶层的话，挪进 src/batbar/_legacy/ 就绕过了）。
_DEAD_BRIDGE = ("wire.py", "net.py", "controller.py")


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _scan() -> list[str]:
    """扫出 src/batbar 下所有「开了监听」的地方。"""
    hits: list[str] = []
    for p in _py_files():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for kind, pat in _PATTERNS.items():
                if pat.search(line):
                    hits.append("%s:%d [%s] %s" % (p.name, i, kind, line.strip()[:80]))
    return hits


class NoListener(unittest.TestCase):
    """源码扫描守门：任何模块都不许再开监听端口。"""

    def test_scan_actually_reaches_sources(self):
        """阳性自证：守卫本身有效 —— 防「零命中 = 永远通过」的假通过。

        若 SRC 算错或 glob 没匹配到任何文件，`_scan()` 会返回 `[]`，
        `assertEqual(hits, [])` 直接通过。这条先证明「确实扫到了一批文件」。
        """
        self.assertTrue(SRC.is_dir(), "扫描根 %s 不存在 —— 守卫在空转" % SRC)
        n = len(_py_files())
        self.assertGreater(n, 10,
                           "只扫到 %d 个 .py —— SRC 路径或 glob 写错了，断言会假通过" % n)

    def test_no_module_binds_or_listens(self):
        hits = _scan()
        self.assertEqual(
            hits, [],
            "这些地方会开监听端口 ⇒ 触发防火墙弹窗，请删掉（batbar 读电量走本地 "
            "HID，完全不需要联网）：\n  " + "\n  ".join(hits))

    def test_no_module_imports_socket(self):
        """整个包都不该再有 socket 的立足点（扫**全包**，与这句话对齐）。

        旧架构里 controller.py 是 TCP 客户端、net.py 查本机 IP —— 二者已随桥接
        一起删除 ⇒ 现在包内任何模块出现 socket 都是回归（ping / 探测自己 IP
        这类需求一律不要加）。
        """
        files = _py_files()
        self.assertTrue(files, "扫不到任何 .py —— 本条会假通过")
        for p in files:
            body = p.read_text(encoding="utf-8")
            self.assertNotIn("import socket", body,
                             "%s 又 import socket 了 —— 包内不该有网络代码" % p.name)
            self.assertNotIn("socket.socket", body,
                             "%s 又建 socket 了 —— 包内不该有网络代码" % p.name)

    def test_no_hardcoded_port_constants(self):
        """`HOST`/`PORT` 这对桥接常量不该回来了（防止顺手抄回旧代码）。"""
        for p in _py_files():
            body = p.read_text(encoding="utf-8")
            self.assertFalse(re.search(r"^PORT\s*=", body, re.M),
                             "%s 又出现了 PORT 常量" % p.name)
            self.assertFalse(re.search(r"^HOST\s*=", body, re.M),
                             "%s 又出现了 HOST 常量" % p.name)

    def test_legacy_bridge_modules_stay_deleted(self):
        """wire.py / net.py / controller.py 不许复活（可从 git 历史里捞回，故需守门）。

        它们在 v0.1.4 被删：v0.1.3 删掉 agent 侧监听后，controller 永远连不上，
        三个文件彻底失去调用方。留着它们**不会**直接弹防火墙（监听不在这儿），
        但会让「谁在跟谁通信」再次变得含糊 —— 正是这种含糊让那个监听多活了三个
        版本。

        按**文件名**在任意层级匹配 —— 只查顶层的话，挪进 `src/batbar/_legacy/`
        就绕过了。
        """
        present = sorted(p.relative_to(SRC).as_posix()
                         for p in SRC.rglob("*") if p.name in _DEAD_BRIDGE)
        self.assertEqual(present, [],
                         "旧双进程桥接已废弃，不要恢复：%s" % ", ".join(present))


if __name__ == "__main__":
    unittest.main()
