"""batbar — 常驻 Windows 系统托盘的多设备电量指示器（鼠标 / 耳机 / 机械键盘）。

纯 Windows 单进程应用：读设备 → 轮询/事件驱动 → Pillow 渲染 → pystray 托盘，
全程在同一个进程里完成，不需要任何 IPC。

分层（单测可跑的前提）：
  * 纯逻辑层（hidpp / session / state / lowbattery / lastknown / winevents /
    fullscreen）——零第三方依赖，可在任意 Python 3.11+ 平台离线单元测试。
  * I/O 层（transport / agent / bt_battery / dongle / poll24 / receiver24 /
    wired24 / win32ctypes / singleinstance / applog）——Windows-only。

> 旧双进程架构的残留模块（wire / net / controller）已不在主路径，
> 详见 ARCHITECTURE.md §2.3。新功能不要引用它们。
"""

__version__ = "0.1.3"
