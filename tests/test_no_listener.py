"""打包后的 exe 不得再开任何监听端口（2026-10-14 用户报的防火墙弹窗）。

起因：旧架构是「Windows 侧 agent + WSL2 侧 controller」双进程，靠本地 TCP 桥
（`0.0.0.0:51788`）通信。后来改成单进程自跑，controller 那条路早已废弃，但
`agent.py` 里 `HOST = "0.0.0.0"` / `PORT = 51788` 和 `_serve()` 没有一起删
⇒ 每次双击 exe，Windows 防火墙就弹一次「是否允许 batbar.exe 访问网络」。

⚠️ 为什么必须靠测试守住，不能靠手测：

1. **防火墙提示**通常**每台机器只弹一次** —— 在本机点过「取消」之后就再也不
   弹了，于是「我这边没看到」是完全没有证明力的（当年黑窗 bug 也是这个德性：
   源码跑复现不出）。
2. 监听的是 `0.0.0.0`（**全部网卡**），不只是 127.0.0.1 ⇒ 等于对局域网开放
   一个没人用的端口。这既是弹窗来源，也是无谓的攻击面。

判定口径：不看「有没有 import socket」（controller 作为**客户端**还要用），
只看**「有没有真的 listen」** —— 禁止 `.bind(` 与 `listen(` 这两个动作。
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


def _scan() -> list[str]:
    """扫出 src/batbar 下所有「开了监听」的地方。"""
    hits: list[str] = []
    for p in sorted(SRC.rglob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for kind, pat in _PATTERNS.items():
                if pat.search(line):
                    hits.append("%s:%d [%s] %s" % (p.name, i, kind, line.strip()[:80]))
    return hits


class NoListener(unittest.TestCase):
    """源码扫描守门：任何模块都不许再开监听端口。"""

    def test_no_module_binds_or_listens(self):
        hits = _scan()
        self.assertEqual(
            hits, [],
            "这些地方会开监听端口 ⇒ 触发防火墙弹窗，请删掉（batbar 读电量走本地 "
            "HID，完全不需要联网）：\n  " + "\n  ".join(hits))

    def test_agent_does_not_import_socket(self):
        """agent 是托盘常驻进程，连 socket 都不该 import（更强的约定）。

        controller.py 作为客户端仍可用 socket —— 所以这条只约束 agent.py。
        """
        body = (SRC / "agent.py").read_text(encoding="utf-8")
        self.assertNotIn("import socket", body)
        self.assertNotIn("socket.socket", body)

    def test_no_hardcoded_port_constants(self):
        """`HOST`/`PORT` 这对桥接常量不该回来了（防止顺手抄回旧代码）。"""
        body = (SRC / "agent.py").read_text(encoding="utf-8")
        self.assertFalse(re.search(r"^PORT\s*=", body, re.M), "agent.py 又出现了 PORT 常量")
        self.assertFalse(re.search(r"^HOST\s*=", body, re.M), "agent.py 又出现了 HOST 常量")


if __name__ == "__main__":
    unittest.main()
