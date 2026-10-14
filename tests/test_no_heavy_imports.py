"""守住「不联网」这条链不再回流（2026-10-14 量化打包体积时立的规矩）。

起因
----
`applog.py` 里一句 `import logging.handlers`（为了用 `RotatingFileHandler`），
把 **9 MB 的 OpenSSL** 拖进了打包产物：

    logging/handlers.py 顶层 import socket
                        函数体 import smtplib / http.client / ssl
    PyInstaller 静态扫描 import 语句，不管它写在函数体还是 try 里 ⇒
    ssl → _ssl.pyd → libcrypto-3-x64.dll(7.49 MB) + libssl-3-x64.dll(1.50 MB)

batbar 全程不联网（`test_no_listener.py` 已证零监听），这 9 MB 纯白给。
去掉它（改成自写滚动 handler）+ 剔除 Pillow 的 AVIF 插件，体积
16.28 MB → 9.29 MB。

★ 为什么必须靠测试守
--------------------
这类回归**编译期几乎无声**：`pip install requests`、或者有人图省事改回
`logging.handlers.RotatingFileHandler`，exe 照样能编出来、照样能跑，
只是悄悄胖 9 MB。唯一明显症状是产物体积，而没人会盯着 CI 的体积看。

所以这里做**两道**检查：
  静态 —— AST 扫 `src/batbar/**/*.py`，禁掉整个网络栈的 import；
  动态 —— 在**干净解释器**里 import batbar，断言重模块没被带进 sys.modules。

⚠️ 两条各自容易犯的错
----------------------
* 静态扫描用**正则**会误伤：本文件、`applog.py` 的 docstring 里都写着
  `import logging.handlers` 这几个字（在解释为什么不能用）。所以这里用 **AST**
  取真正的 import 语句，注释和文档字符串一概不算。
* 扫描型守卫最危险的失败模式是「**永远通过**」：SRC 路径写错、glob 一个文件
  都没匹配 ⇒ 空列表 ⇒ 断言直接过 ⇒ 比没有守卫更坏（虚假安全感）。所以每条
  扫描断言都先自证「扫到了东西」（见 `test_scan_reaches_sources`）。
* 动态检查必须在**子进程**里做：pytest 同一个进程里跑了几百个用例，谁知道
  哪个依赖已经 `import socket` 了 —— 在本进程断言 `sys.modules` 是假阳性。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

# 静态扫描用 AST（不能用子串匹配，理由见 scanutil 模块 docstring）
import scanutil

SRC = Path(__file__).resolve().parents[1] / "src" / "batbar"

#: 禁止 import 的模块（精确匹配 import 语句里的模块名）
#: —— 每一条都对应"拖进来一大坨用不到的东西"或"违反不联网约定"
FORBIDDEN = {
    "logging.handlers": "拖进 OpenSSL（9 MB），见 applog.py 模块 docstring",
    "ssl": "OpenSSL，batbar 不联网",
    "_ssl": "OpenSSL 的 C 扩展",
    "socket": "网络栈入口；batbar 零监听，不该有任何网络动作",
    "_socket": "socket 的 C 扩展",
    "smtplib": "发邮件，与本项目无关",
    "http.client": "HTTP 客户端；且被 urllib.request 顶层依赖",
    "urllib.request": "会连带 http.client / ssl",
    "ftplib": "FTP，与本项目无关",
    "imaplib": "收邮件，与本项目无关",
    "poplib": "收邮件，与本项目无关",
    "telnetlib": "Telnet，与本项目无关",
    "requests": "第三方 HTTP 库，会拖进 urllib3 + certifi + ssl",
}


class StaticScan(unittest.TestCase):
    def test_scan_reaches_sources(self):
        """自证扫描确实扫到了文件 —— 防空列表导致的假通过。"""
        files = scanutil.py_files(SRC)
        self.assertGreater(len(files), 10, f"没扫到源码，SRC 错了？{SRC}")
        self.assertTrue((SRC / "applog.py").is_file())

    def test_no_forbidden_imports(self):
        hits: list[str] = []
        for f in scanutil.py_files(SRC):
            roots = scanutil.imported_roots(f)
            for m in sorted(roots & set(FORBIDDEN)):
                hits.append(f"{f.relative_to(SRC)}: import {m} —— {FORBIDDEN[m]}")
        self.assertEqual([], hits, "\n".join(hits))

    def test_scanner_actually_detects_a_forbidden_import(self):
        """阳性对照：确认上面那条断言不是「永远通过」。"""
        sample = "import logging\nimport logging.handlers\nimport ssl\n"
        roots = scanutil.imported_roots_from_source(sample)
        self.assertIn("logging.handlers", roots)
        self.assertIn("ssl", roots)

    def test_scanner_ignores_mentions_in_docstrings(self):
        """阴性对照：文档里提到 `import logging.handlers` 不该被判违规。"""
        sample = '"""别用 import logging.handlers，会拖进 ssl 和 socket。"""\n'
        roots = scanutil.imported_roots_from_source(sample)
        self.assertEqual(set(), roots & set(FORBIDDEN))


class RuntimeNoHeavyImports(unittest.TestCase):
    """在**干净解释器**里 import batbar，确认重模块没被带进来。"""

    #: 精确名单，**不要**写成 `m.startswith('urllib.')` 之类的前缀匹配：
    #: Python 3.12 的 `pathlib` 会 `from urllib.parse import ...`，`urllib.parse`
    #: 是纯 Python、不碰 ssl，属于正常且无法避免的 —— 前缀匹配会把它误判成违规，
    #: 然后要么有人为了"修"它去禁用 pathlib，要么直接把这条守卫删掉，两种都比
    #: 没有守卫糟。
    NAMES = ("logging.handlers", "ssl", "_ssl", "socket", "_socket",
             "smtplib", "http.client", "urllib.request",
             "ftplib", "imaplib", "poplib", "telnetlib", "requests")

    CHECK = (
        "import sys;"
        "import batbar.applog, batbar.agent;"
        f"print(','.join(sorted(m for m in sys.modules if m in {NAMES!r})))"
    )

    def _run(self) -> list[str]:
        root = Path(__file__).resolve().parents[1]
        env_src = str(root / "src")
        proc = subprocess.run(
            [sys.executable, "-c", self.CHECK],
            capture_output=True, text=True, cwd=str(root),
            env={"PATH": __import__("os").environ.get("PATH", ""),
                 "PYTHONPATH": env_src,
                 "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
                 "TEMP": __import__("os").environ.get("TEMP", ""),
                 "TMP": __import__("os").environ.get("TMP", "")},
            check=False,
        )
        if proc.returncode != 0:
            self.fail(f"子进程检查失败（退出码 {proc.returncode}）：\n{proc.stderr}")
        return [m for m in proc.stdout.strip().split(",") if m]

    def test_heavy_modules_absent(self):
        present = self._run()
        self.assertEqual([], present,
                         "这些模块又被拖进来了（多半是有人改回 logging.handlers）："
                         + ", ".join(present))

    def test_check_is_not_vacuous(self):
        """阳性对照：走**同一段** CHECK，只是开头多一句 `import logging.handlers`。

        必须能报出东西 —— 否则说明 CHECK 写错了（比如名字拼错、sys.modules 查错），
        上面那条断言就会变成「永远通过」的假守卫。
        """
        root = Path(__file__).resolve().parents[1]
        mutated = "import logging.handlers;" + self.CHECK
        proc = subprocess.run(
            [sys.executable, "-c", mutated],
            capture_output=True, text=True, cwd=str(root),
            env={"PATH": __import__("os").environ.get("PATH", ""),
                 "PYTHONPATH": str(root / "src"),
                 "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
            check=False,
        )
        self.assertEqual(0, proc.returncode, f"阳性对照自己没跑起来：{proc.stderr}")
        self.assertIn("logging.handlers", proc.stdout,
                      "CHECK 抓不到已经 import 的模块 —— 守卫是假的")


class HashlibWithoutOpenSSL(unittest.TestCase):
    """`_hashlib`（OpenSSL 的 C 扩展）被排除后，`hashlib` 必须还能用。

    为什么排除它：`_hashlib.pyd` **静态链接 `libcrypto-3-x64.dll`**（7.49 MB），
    只要它留在包里，OpenSSL 就永远跟着进来。而 batbar 从不直接调 hashlib，
    唯一的间接用户是 `random`（`from hashlib import sha512`）。

    安全的前提：CPython 3.12 把 `_md5` / `_sha1` / `_sha2` / `_sha3` / `_blake2`
    **全部编译进了 python312.dll**，hashlib 缺了 OpenSSL 会自动降级到它们，
    只损失 `pbkdf2_hmac` / `scrypt`（batbar 用不到）。

    ⚠️ 这个前提**会随 Python 版本变**：哪天这些模块不再是 builtin，删掉
    `_hashlib` 就会让 `import random` 直接炸 —— 所以下面钉住它。
    """

    BUILTINS = ("_md5", "_sha1", "_sha2", "_sha3", "_blake2")

    def test_hash_backends_are_builtin(self):
        missing = [m for m in self.BUILTINS if m not in sys.builtin_module_names]
        self.assertEqual([], missing,
                         "这些哈希后端不再是内置模块 —— 排除 _hashlib 的前提没了，"
                         "必须恢复 _hashlib + libcrypto，否则 import random 会炸")

    def test_hashlib_works_without_openssl(self):
        """阳性对照兼可行性验证：把 _hashlib 屏蔽掉，hashlib/random 仍可用。"""
        code = (
            "import sys, importlib, hashlib;"
            "sys.modules['_hashlib'] = None;"          # 让 import _hashlib 抛 ImportError
            "importlib.reload(hashlib);"
            "print(hashlib.sha256(b'abc').hexdigest());"
            "import random; random.random(); print('random-ok')"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, check=False,
                              env={"SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
        self.assertEqual(0, proc.returncode, f"无 OpenSSL 的 hashlib 跑不起来：{proc.stderr}")
        out = proc.stdout.split()
        self.assertEqual("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                         out[0], "sha256 结果不对（降级路径没生效？）")
        self.assertIn("random-ok", out)


if __name__ == "__main__":
    unittest.main()
