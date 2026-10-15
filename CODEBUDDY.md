# CODEBUDDY.md — batbar 项目通用偏好

> 本文件位于项目根目录，会被 CodeBuddy（独立 IDE 与 PyCharm 等编辑器插件）在每次对话时自动纳入上下文。
> 它与账号无关、随 Git 仓库共享，因此在不同机器、不同 CodeBuddy 形态下都能保持一致。

## 项目概况
- batbar：常驻 Windows 系统托盘的多设备电量指示器（鼠标 / 耳机 / 机械键盘）。
- 纯 Python 3.11+，使用 `uv` 做依赖与虚拟环境管理。
- 打包后端为 `hatchling`， Wheel 入口 `src/batbar`。

## 技术栈与依赖约定
- **基础逻辑层**：不引入任何第三方依赖，确保任意 Python 3.11+ 可直接运行。
- **agent（Windows 运行时）**：`uv sync --extra agent` 安装 `hidapi`（cython-hidapi）、`pystray`、`Pillow`。
- **dev**：`pytest>=8` + `pytest-timeout`。
- 仅在 Windows 平台需要的依赖务必加 `sys_platform == 'win32'` 标记。

## 代码风格（强制遵守）
- 行宽 `line-length = 110`（ruff 配置），不要按 79/88 自行换行。
- 目标版本 `py311`，可使用 3.11+ 语法（如 `match`、异常组）。
- 遵循 ruff 默认规则；提交前跑 `ruff check .` 与 `ruff format .`。
- 纯逻辑模块保持无第三方 import，便于跨平台测试。

## 测试约定
- 测试目录 `tests/`，用 `pytest` 运行（`testpaths = ["tests"]`，`timeout = 60`，`-q`）。
- 涉及 HID/托盘的 Windows 专属逻辑要区分平台，避免在非 Windows CI 中触发。
- 新增功能请同步补充测试。

## 用户偏好（记忆类，两端共享）
<!-- 把下面的偏好改成你自己的，AI 在 IDE 与 PyCharm 插件中都会遵循 -->
- 注释与说明使用简体中文。
- 优先给出最小可运行修改，避免大范围重构。
- 解释代码时先讲"为什么"，再给"怎么做"。
- 涉及 Windows 专属 API 时，提示跨平台兼容性风险。

## 沟通偏好
- 回答简洁、直接，给出可执行的命令或代码块。
- 改动前先说明影响范围（哪些文件、是否破坏现有测试）。
