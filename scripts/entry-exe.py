"""PyInstaller 打包入口 —— 只给打包用，源码运行不需要它。

为什么不能直接把 agent.py 喂给 PyInstaller：agent.py 内部全是包内相对导入
（`from . import applog`），被当成顶层脚本执行时会炸：
    ImportError: attempted relative import with no known parent package
这个 stub 只做一件事 —— 以**包**的身份导入 `batbar.agent`，再调 `main()`。

等价关系：
    源码运行    python -m batbar.agent
    打包后      batbar.exe
"""
from batbar.agent import main

if __name__ == "__main__":
    main()
