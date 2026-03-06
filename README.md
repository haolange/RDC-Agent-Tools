# `rdx-tools`

`rdx-tools` 是面向 `RenderDoc` 的本地 `MCP` + `CLI` 工具集，用于暴露稳定的 `rd.*` tool 能力、管理 `.rdc` 到 replay session 的运行时链路，并为人工与自动化调用提供统一入口。

本仓库关注“平台层使用模型”：

- 提供 `CLI`、`MCP`、tool catalog 与 runtime 约束。
- 说明如何把一份 `.rdc` 变成可操作的 session。
- 说明 `context`、daemon、session state、artifact 的关系。

本仓库**不**提供上层业务 workflow：

- 不描述 shader debug、reverse、analysis、optimize 等任务策略。
- 不替代上层 skills、system prompt、reference docs。
- 不规定 Agent 必须按哪条业务链路组合 tools。

## 规范源优先级

理解本仓库时，请按以下顺序判断“什么是平台定义”：

1. `spec/tool_catalog_196.json` 与共享响应契约
2. runtime 实际行为
3. `CLI` 对常见平台动作的封装

这意味着：

- tool 能力面与参数语义，以 catalog 和共享契约为准。
- runtime 行为是平台真相的运行时体现。
- `CLI` 只是 convenience wrapper，不是完整能力面的等价镜像，也不是规范源。

## 入口概览

### `rdx.bat`

统一 launcher，适合人工使用。

- 默认模式：交互式入口，提供 `Start CLI`、`Start MCP`、`Help`。
- `--non-interactive`：脚本与自动化入口。

```bat
rdx.bat
rdx.bat --non-interactive mcp --ensure-env
```

### `cli/run_cli.py`

命令行入口，适合人工调试、脚本回归、最小链路验证。

- 面向“命令”而不是 tool schema。
- 负责把常见平台动作封装成 `capture open`、`capture status`、`daemon status` 等命令。

### `mcp/run_mcp.py`

`MCP` server 入口，适合被外部 `MCP` client / Agent 接入。

- 暴露 196 个 `rd.*` tools。
- 由上层 client 进行 tool discovery、参数组织与调用编排。

## 最小示例

下面的示例只演示我已在当前仓库中直接验证过的平台级最小入口：

```bat
python cli/run_cli.py capture open --file "C:\path\capture.rdc" --frame-index 0
python mcp/run_mcp.py --ensure-env --daemon-context smoke-test
```

文档示例默认按顺序执行语义编写。除非显式声明支持并发，否则不应把并发观测结果视为平台定义。
当前 remote debug 主链路也已明确化：

- `rd.remote.connect` 返回的是 live `remote_id`，不是占位 handle。
- `rd.remote.ping` 用于确认该 `remote_id` 仍然连着 live endpoint。
- `rd.capture.open_replay` 需要通过 `options.remote_id` 显式进入 remote replay backend。
- Android remote 可通过 `rd.remote.connect` 的 `options.transport="adb_android"` 触发仓库内置的 `adb` bootstrap。

更完整的操作说明见 [docs/quickstart.md](docs/quickstart.md)。

## 文档导航

- [docs/README.md](docs/README.md)：文档导航与阅读顺序
- [docs/quickstart.md](docs/quickstart.md)：最短上手路径
- [docs/session-model.md](docs/session-model.md)：`.rdc` 到 session 的平台模型
- [docs/agent-model.md](docs/agent-model.md)：上层 Agent / framework 使用原则
- [docs/doc-governance.md](docs/doc-governance.md)：文档自更新治理、量化指标与影响面映射
- [docs/configuration.md](docs/configuration.md)：runtime layout、环境变量、根目录约束
- [docs/troubleshooting.md](docs/troubleshooting.md)：常见故障与恢复
- [docs/tools.md](docs/tools.md)：tool catalog 入口与校验方式

## 关键约束

- tool catalog 的权威来源是 `spec/tool_catalog_196.json`。
- catalog 必须且只能包含 196 个 `rd.*` tools。
- 运行时响应遵循共享契约；调试时优先检查 `ok` 与 `error_message`。
- 默认参考根目录由 `rdx.bat` 或脚本自身位置推导；`RDX_TOOLS_ROOT` 仅用于覆盖默认值。

## 验证

```bat
python spec/validate_catalog.py
python cli/run_cli.py --help
python mcp/run_mcp.py --help
python scripts/check_markdown_health.py
```
