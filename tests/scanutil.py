"""源码静态扫描的共用工具（供 test_no_listener / test_no_heavy_imports 使用）。

★ 为什么必须用 AST 而不是子串匹配
----------------------------------
`test_no_listener.py` 原来写的是 `assertNotIn("import socket", body)` ——
结果 `applog.py` 的 **docstring** 里为了解释「为什么不能用 logging.handlers」，
原样写了 CPython `logging/handlers.py` 顶层 `import socket` 这行字，
守卫立刻误报（2026-10-14 真踩到）。

这类「解释性文字里引用了被禁代码」的情况以后只会更多：禁令写得越清楚，
文档里出现的次数越多。所以判定只能看**真正的 import 语句**（AST），
注释和文档字符串一概不算。

同时保留 `has_text()` 给那些「动作型」检查用（比如 `socket.socket(` 这种
调用写法），它们的语义不在 import 语句里。
"""
from __future__ import annotations

import ast
from pathlib import Path


def py_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def imported_roots(path: Path) -> set[str]:
    """一个文件里所有 import 语句引用的模块名（含 `import a.b` 的 `a.b`）。"""
    return imported_roots_from_source(path.read_text(encoding="utf-8"))


def imported_roots_from_source(src: str) -> set[str]:
    tree = ast.parse(src)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            # 相对导入（level > 0）是本包内部的事，不算外部依赖
            if node.level == 0 and node.module:
                out.add(node.module)
    return out


def has_text(body: str, needle: str) -> bool:
    return needle in body
