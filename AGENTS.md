# AGENTS：rdx-tools

## 范围

本目录是独立分发的 `rdx-tools`。

- 覆盖范围：RenderDoc 的 MCP/CLI 运行时工具（`rdx-tools` 包）。
- 排除范围：除非用户明确提出，否则不涉及本目录树以外的其他文件夹。

## 根目录约束（强约束）

- `rdx.bat` 所在目录是唯一允许的参考根目录。
- 所有路径解析必须以 `rdx.bat` 所在目录为根，不允许依赖父目录或同级目录结构。
- 禁止硬编码绝对路径（例如 `D:\Projects\...`）以及 `..\` 形式的父目录逃逸。
- 启动脚本与运行时代码应优先从脚本自身位置或 `RDX_TOOLS_ROOT` 推导路径。`RDX_TOOLS_ROOT` 仅用于覆盖默认根目录；默认参考根目录就是 `rdx.bat` 所在目录。
- 非用户明确要求时，不得读取或写入以 `rdx.bat` 所在目录为根的目录树之外的文件。
- 如果发现现有逻辑引用了参考根目录之外的路径，应先改为根内相对/派生路径再继续功能开发。

## 文档语言与格式规范（中文为主，保留英文术语）

- 本仓库目录树内的 `*.md` 文档以中文为主。
- 必须保留英文（并用反引号包裹）：`RenderDoc`、`rdx-tools`、`MCP`、`CLI`、`rdx.bat`、命令行示例、环境变量（例如 `RDX_*`）、文件/目录路径、代码标识符、`rd.*` tool names、JSON/YAML key。
- 专业名词首次出现可采用“中文说明 + 英文术语”的写法，但不要把英文术语翻译成中文来替代原英文。
- 代码块内容不改语义、不改命令；仅在代码块外用中文补充说明。
- 强约束：非代码块正文、标题、列表项中，不得保留可直接改写为中文的整句英文或整段英文说明；新增或更新 `*.md` 时，默认先写中文说明，再嵌入必要英文术语。
- 强约束：如果必须引用英文原词，允许保留英文术语本体，但其外围解释、上下文承接与结论必须使用中文，不得用整段英文替代中文说明。
- 文档审阅时，必须显式检查“是否仍存在可中文化但保留为整句英文的内容”；如存在，应先中文化再交付。
- 编码（强约束）：本仓库内所有 `*.md` 必须使用 UTF-8 with BOM，禁止提交为 ANSI、GBK、UTF-8 without BOM 等其他编码；否则 GitHub 页面容易出现乱码。

## 关键入口

- `rdx.bat` 启动器：
  - `rdx.bat [--non-interactive] mcp ...`
  - `rdx.bat [--non-interactive] cli ...`
- `mcp/run_mcp.py`
- `cli/run_cli.py`
- `scripts/release_gate.py`

## `scripts/` 治理

- `scripts/` 目录只保留正式的平台 / 治理脚本。
- 受支持的脚本集合以 `scripts/README.md` 为准。
- 禁止把个人机器路径、个人 `adb.exe`、设备 serial 或桌面样本路径硬编码到正式脚本默认值中。
- 一次性调查脚本、单样本探针、参数实验脚本、仅用于复盘的报告生成脚本，不属于主仓库正式接口。
- 如果正式脚本集合或脚本接口发生变化，必须在同一次交付中同步更新 `scripts/README.md`、相关文档与测试。
- `python scripts/release_gate.py --require-smoke-reports` 属于真 smoke 门禁：不允许只靠报告文件存在性通过，必须以当前 smoke truth 为准。

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

- tool catalog 来源：`spec/tool_catalog.json`。
- catalog 当前数量由 `spec/tool_catalog.json` 的 `tool_count` 定义；当前为 `190`。如后续继续增删，必须同步更新 validator、帮助输出、文档与测试口径。
- tool 名称为规范的 `rd.*` tool names。
- 运行时响应遵循 `rdx/core/contracts.py` 中的共享契约：
  - 调试时优先检查 `ok` 与 `error_message`。
- 除非用户明确要求修改代码，否则将编辑限制在文档/配置文档范围内。

## 文档自更新治理（强约束）

### 触发条件

只要改动影响以下任一维度，就必须同步检查并按需更新文档：

- 新增、删除、重命名 `CLI` 命令或参数。
- 新增、删除、重命名 `MCP` transport、入口行为、交互语义。
- 新增、删除、重命名 `rd.*` tool、tool 参数、tool 返回字段或能力边界。
- 改变 `.rdc -> session` 链路、`context`、daemon、session state、artifact 语义。
- 改变错误面、恢复路径、帮助输出、运行时前置条件、环境变量。

### 最低文档更新粒度

- 不允许只改一处主文档就交付。
- 每次影响平台模型的功能改动，至少要检查：
  - `README.md`
  - `docs/quickstart.md`
  - `docs/session-model.md`
  - `docs/agent-model.md`
  - `docs/troubleshooting.md`
  - `docs/tools.md`
  - `docs/doc-governance.md`
- 未受影响可不改，但必须在交付说明中人工确认“为何不改”。

### 交付前量化门槛

- 文档必须通过：
  - `python scripts/check_markdown_health.py`
- 若改动涉及入口或契约，必须重新跑：
  - `python spec/validate_catalog.py`
  - `python cli/run_cli.py --help`
  - `python mcp/run_mcp.py --help`
- 若改动涉及 `.rdc` 会话链路，必须至少顺序验证一次：
  - `capture open`
  - `capture status` 或等价状态读取链路

### 交付说明要求

交付时必须说明：

- 更新了哪些文档。
- 哪些文档确认无需更新，以及原因。
- 文档验证跑了哪些命令。
- 清理结果。

### 口径底线

- 规范源优先级必须与仓库主文档一致：catalog / contract 优先，runtime 其次，`CLI` 不是规范源。
- `capture_file_id`、`session_id` 等 handle 必须写成运行时引用，不得暗示长期稳定。
- 文档示例默认按顺序执行语义编写；除非明确声明支持并发，否则不得把并发现象写成平台定义。
- 未验证的行为不得写成“已验证”。

详见 `docs/doc-governance.md`。

## 开发 Agent 自测引导（强约束）

如果是开发 Agent 自身完成了平台开发与本地自测，不允许只根据用户随手给的一条命令就结束。应先回看这些第一性文档，再按改动面自行设计验证流程并执行：

- `README.md`
- `docs/session-model.md`
- `docs/agent-model.md`
- `docs/troubleshooting.md`
- `docs/doc-governance.md`
- 若涉及 Android / remote / transport / 大面 smoke，再看 `docs/android-remote-cli-smoke-prompt.md`

- `Conflict policy:` 如果实现与这些第一性文档冲突，开发 Agent 必须在本次交付中同步协调代码与文档。

开发 Agent 完成开发后，至少应按改动面自检：

- 入口 / help / catalog 改动
  - 必须跑：
    - `python spec/validate_catalog.py`
    - `python cli/run_cli.py --help`
    - `python mcp/run_mcp.py --help`
- session / context / daemon 改动
  - 必须至少顺序验证一次：
    - `capture open`
    - `capture status` 或等价状态读取链路
  - 若当前库已公开 `rd.session.get_context` / `rd.session.update_context`，也应补一条 context 读取或更新验证。
- remote / bootstrap 改动
  - 必须至少顺序验证一次：
    - `rd.remote.connect`
    - `rd.remote.ping`
    - `rd.capture.open_replay(options.remote_id=...)`
  - 若 remote replay 成功，还应确认旧 `remote_id` 的 consumed 语义符合预期。
- tool schema / error contract 改动
  - 必须至少补一条代表性 tool 调用与一条代表性失败面验证，确认 `ok`、`error_message`、`error.details` 口径一致。
- 大面改动或跨 transport 改动
  - 不得只跑最短链路；必须参考 `docs/android-remote-cli-smoke-prompt.md` 自行组织分层 smoke / contract 流程。

开发 Agent 交付时必须说明：

- 它参考了哪些第一性文档。
- 它为何选择当前测试流程。
- 哪些链路已验证，哪些链路因环境限制未验证。
- 清理结果：`已清理` 或 `未完全清理（含原因）`。
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
   - `python scripts/check_markdown_health.py`
3. 只提交有意图且可解释的变更。

## 版本控制约定

- 不要提交生成的运行时输出（例如 `intermediate/**`、日志、临时构建文件）。
- 不要提交 `*.pyc`、`.pytest_cache`、`__pycache__`。



