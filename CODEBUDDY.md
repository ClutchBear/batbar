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
- **dev**：`pytest>=8` + `pytest-timeout` + `ruff>=0.17`。
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

## 环境与工具链（本机命令速查）

- **建环境**：`uv sync --extra agent --extra dev`。⚠️ 裸 `uv sync` 只装 dev 组，会丢掉 agent 组的
  hidapi / pystray / Pillow，托盘直接跑不起来。
- **打包工具**：`pyinstaller` 刻意不进 pyproject（只有打包用得到），所以每次 `uv sync` 之后要补
  `uv pip install pyinstaller` —— uv sync 按锁文件精确同步，会把它清掉。
- **跑测试**：`.venv/Scripts/python.exe -m pytest`（纯逻辑、无需 HID 硬件；项数以 README 徽章为准，
  改测试数量时要同步 README / ARCHITECTURE / CI 注释）。
- **格式化**：`uvx ruff check .` / `uvx ruff format .`（行宽 110，见 `[tool.ruff]`）。
- **打包**：`.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean batbar.spec`。
- **真机验证**：`.venv/Scripts/python.exe -m batbar.agent --probe --allow-multiple`。

> 终端自动激活 venv 由 `~/.bashrc` 里的一段 `_auto_venv` 负责（从当前目录向上找 `.venv` 并激活，
> 离开则退出）。该配置**在 HOME 目录、不进仓库**，换机器需要重新配置。

## 发版流程（低频，但一步做错代价大）

1. **版本号 4 处必须同步**：`pyproject.toml`、`src/batbar/__init__.py`、
   `scripts/version_info.txt`（`FileVersion` + `ProductVersion`）、`uv.lock`（改完跑 `uv lock`）。
2. **提交信息走文件**：`git commit -F _tmp/commitmsg.txt`。⚠️ 别用 `-m` 写带反引号的中文长消息，
   bash 会把反引号当命令替换执行，消息会被吃掉一块。
3. **tag 同理**：`git tag -a vX.Y.Z -F _tmp/tagmsg.txt`。
4. 🔴 **未经用户明确确认，不打 tag、不发 Release** —— 推 tag 会触发
   `.github/workflows/build-exe.yml` 跑测试 → 打包 → `gh release create`，公开后不可逆。
5. `gh` 本机未登录时查 CI：`git credential fill` 取 token → `export GH_TOKEN=...` →
   `gh run watch <run-id>`（别写 20 秒轮询的长脚本，匿名 API 只有 60 次/小时）。

## 验证链（发版前逐项过）

| 步骤 | 判据 |
|---|---|
| 单测 | pytest 全绿（无需硬件，CI 上能真跑） |
| ruff | `ruff format --check .` 全部已格式化；`ruff check` 的历史告警不新增 |
| 产物 | `FileVersion` 与 `pyproject.toml` 的 `version` 一致；体积落 8–40 MB（当前约 9.19 MB） |
| 真机 | `--probe` 读到电量、托盘图标正常 |

## 已知坑（踩过，别再踩）

- **项目目录改名后激活脚本会失效**：venv 的 `Scripts/activate*` 里 `VIRTUAL_ENV` 是**创建时硬编码**
  的绝对路径，改名/移动后仍指向旧路径 —— 激活只是往 PATH 里塞一个死路径，`python` 静默落到系统
  Python，现象是「终端进不了虚拟环境但没报错」。`uv sync` **不会**重写它 ⇒ 必须删 `.venv` 重建。
- **PyCharm 保存会插空行**：遇到「只多/少几个空行」的神秘改动，先怀疑是 PyCharm 自己存的
  （`agent.py`、`pyproject.toml` 都出现过），不是并行会话也不是文件损坏。
- **`.venv/` 的 gitignore 规则不覆盖改名目录**（如临时备份 `.venv.stale-*`），会被 git 当未跟踪项，
  清理后记得检查 `git status`。
