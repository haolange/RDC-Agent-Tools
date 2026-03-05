# AGENTS：rdx-tools

## 范围

本目录是独立分发的 `rdx-tools`。

- 覆盖范围：RenderDoc 的 MCP/CLI 运行时工具（`rdx-tools` 包）。
- 排除范围：除非用户明确提出，否则不涉及本目录树以外的其他文件夹。

## 根目录约束（强约束）

- `rdx-tools/` 是唯一允许的顶层参考根目录。
- 所有路径解析必须以 `rdx-tools` 为根，不允许依赖父目录或同级目录结构。
- 禁止硬编码绝对路径（例如 `D:\Projects\...`）以及 `..\` 形式的父目录逃逸。
- 启动脚本与运行时代码应优先从脚本自身位置或 `RDX_TOOLS_ROOT` 推导路径。
- 非用户明确要求时，不得读取或写入 `rdx-tools/` 目录树之外的文件。
- 如果发现现有逻辑引用了 `rdx-tools` 外部路径，应先改为根内相对/派生路径再继续功能开发。

## 文档语言与格式规范（中文为主，保留英文术语）

- `rdx-tools/**/*.md` 文档以中文为主。
- 必须保留英文（并用反引号包裹）：`RenderDoc`、`rdx-tools`、`MCP`、`CLI`、`rdx.bat`、命令行示例、环境变量（例如 `RDX_*`）、文件/目录路径、代码标识符、`rd.*` tool names、JSON/YAML key。
- 专业名词首次出现可采用“中文说明 + 英文术语”的写法，但不要把英文术语翻译成中文来替代原英文。
- 代码块内容不改语义、不改命令；仅在代码块外用中文补充说明。
- 编码：`rdx-tools` 下所有 `*.md` 必须使用 UTF-8 with BOM。

## 关键入口

- `rdx.bat` 启动器：
  - `rdx.bat [--non-interactive] mcp ...`
  - `rdx.bat [--non-interactive] cli ...`
- `mcp/run_mcp.py`
- `cli/run_cli.py`
- `scripts/release_gate.py`

## 必须保留的目录结构

以下路径为运行时所需，不得删除：

- `rdx/`
- `cli/`
- `mcp/`
- `spec/`
- `policy/`
- `docs/`
- `tests/`
- `scripts/`
- `binaries/windows/x64/`
- `binaries/windows/x64/pymodules/`
- `intermediate/runtime/rdx_cli/`
- `intermediate/artifacts/`
- `intermediate/pytest/`
- `intermediate/logs/`

## 协议与运行时契约

- tool catalog 来源：`spec/tool_catalog_196.json`。
- catalog 必须且只能包含 **196** 个 tools。
- tool 名称为规范的 `rd.*` tool names。
- 运行时响应遵循 `rdx/core/contracts.py` 中的共享契约：
  - 调试时优先检查 `ok` 与 `error_message`。
- 除非用户明确要求修改代码，否则将编辑限制在文档/配置文档范围内。

## 交付前验证（强约束）

- 每次开发改动完成后，必须自行运行相关入口命令进行验证，不可只做静态修改即交付。
- 验证过程中必须结合 terminal 输出做观测与控制，出现报错/异常时先修复再复测，直到结果稳定。
- 至少覆盖本次改动直接影响的主路径（启动、核心命令、关键交互），并确认无阻断性错误。
- 交付标准：可运行、可用、行为符合预期；不带已知阻断 bug。
- 若受环境限制无法完成验证，必须在交付时明确说明未验证项、原因与风险。

## 自检与测试清理（强约束）

- 自检优先使用不会创建持久窗口/后台任务的命令；无必要时禁止触发 `start`、`/k`、常驻 `daemon` 路径。
- 在命令行环境下执行需要用户输入的命令行路径时，严禁直接依赖人工阻塞输入：
  - 命令层回归应使用可编排输入（如管道/`%TEMP%` 输入文件）提供明确输入序列，并限定超时；
  - 若测试流程包含 `pause`、菜单或输入提示，必须先将输入注入或直接校验非交互参数路径，避免将交互等待误判为程序假死。
- 若必须验证“新开窗口”或“常驻进程”路径，必须使用可唯一识别的标题或 `context`，并在验证结束后立即清理。
- 清理范围至少包括：
  - 本次验证打开的 `cmd` 新窗口；
  - 本次验证启动的 `daemon`（含自定义 `--daemon-context`）；
  - 本次验证派生的后台子进程（如 pipe server / python worker）。
- 测试产生的临时文件与临时资源必须在验证结束后清理（例如 `%TEMP%` 下测试日志、临时状态文件、临时管道/锁、临时目录）；仅允许保留仓库明确要求的产物与报告。
- 禁止在交付时留下“隐藏进程占用”或“无人值守窗口”；若清理失败，必须在交付说明中明确列出残留项、影响和手动清理命令。
- 交付说明必须包含一句清理结果：`已清理` 或 `未完全清理（含原因）`。

## 运行时前置条件

`binaries/windows/x64` 必须包含：

- `renderdoc.dll`
- `renderdoc.json`
- `pymodules/renderdoc.pyd`

`intermediate/` 内容为运行时产物，应视为非源码材料。

## 常用环境变量

- `RDX_TOOLS_ROOT`
- `RDX_RENDERDOC_PATH`
- `RDX_ARTIFACT_DIR`
- `RDX_LOG_LEVEL`
- `RDX_DATA_DIR`

## 贡献流程

1. 只修改完成任务所必需的内容。
2. 每次开发完成后，针对本次改动范围执行本地运行与校验，并结合 terminal 输出观测结果：
   - `python spec/validate_catalog.py`
   - `python cli/run_cli.py --help`
   - `python mcp/run_mcp.py --help`
3. 只提交有意图且可解释的变更。

## 版本控制约定

- 不要提交生成的运行时输出（例如 `intermediate/**`、日志、临时构建文件）。
- 不要提交 `*.pyc`、`.pytest_cache`、`__pycache__`。
