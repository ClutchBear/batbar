"""batbar — 常驻 Windows 系统托盘的多设备电量指示器（鼠标 / 耳机 / 机械键盘）。

纯 Windows 单进程应用：读设备 → 轮询/事件驱动 → Pillow 渲染 → pystray 托盘，
全程在同一个进程里完成，不需要任何 IPC。

分层（单测可跑的前提）：
  * 纯逻辑层（hidpp / session / state / lowbattery / lastknown / winevents /
    fullscreen）——零第三方依赖，可在任意 Python 3.11+ 平台离线单元测试。
  * I/O 层（transport / agent / bt_battery / dongle / poll24 / receiver24 /
    wired24 / win32ctypes / singleinstance / applog）——Windows-only。

旧双进程架构（agent + controller 走本地 TCP 桥）已于 v0.1.4 整套删除，
包括 wire.py / net.py / controller.py 及配套单测。不要把它们抄回来 ——
它们是 controller 侧的东西，v0.1.3 删掉 agent 侧监听后 controller 永远连不上，
留着只会占地方、并让「谁在跟谁通信」这件事再次变得含糊。

⚠️ 注意分辨：**弹防火墙的是 `agent.py` 里那个 `0.0.0.0:51788` 监听**（v0.1.3 已删），
不是这三个文件 —— controller.py 是 TCP **客户端**，wire.py 只是编解码，它们从没
监听过任何端口。别把守卫指向错的地方（判据见 tests/test_no_listener.py）。
"""

__version__ = "0.1.7"
