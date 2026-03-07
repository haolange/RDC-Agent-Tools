# `rdx-tools`

`rdx-tools` 是面向 `RenderDoc` 的本地 `MCP` + `CLI` 工具集，用于暴露稳定的 `rd.*` tool 能力、管理 `.rdc` 到 replay session 的运行时链路，并为人工与自动化调用提供统一入口。

本仓库关注“平台层使用模型”：

- 提供 `CLI`、`MCP`、tool catalog 与 runtime 约束。
- 说明如何把一份 `.rdc` 变成可操作的 session。
- 说明 `context`、daemon、session state、artifact、context snapshot 的关系。

本仓库**不**提供上层业务 workflow：

- 不描述 shader debug、reverse、analysis、optimize 等任务策略。
- 不替代上层 skills、system prompt、reference docs。
- 不规定 Agent 必须按哪条业务链路组合 tools。

## 规范源优先级

理解本仓库时，请按以下顺序判断“什么是平台定义”：

1. `spec/tool_catalog.json` 与共享响应契约
2. runtime 实际行为
3. `CLI` 对常见平台动作的封装

这意味着：

- tool 能力面与参数语义，以 catalog 和共享契约为准。
- runtime 行为是平台真相的运行时体现。
- `CLI` 只是 convenience wrapper，不是完整能力面的等价镜像，也不是规范源。
- 规范定义以 `spec/tool_catalog.json` 为准。

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

- 暴露 catalog 当前定义的全部 `rd.*` tools；当前 `tool_count` 为 `198`。
- 已公开包含 `rd.session.get_context` 与 `rd.session.update_context`。
- 由上层 client 进行 tool discovery、参数组织与调用编排。

## 最小示例

下面的示例只演示平台级最小入口：

```bat
python cli/run_cli.py capture open --file "C:\path\capture.rdc" --frame-index 0
python mcp/run_mcp.py --ensure-env --daemon-context smoke-test
```

文档示例默认按顺序执行语义编写。除非显式声明支持并发，否则不应把并发观测结果视为平台定义。

当前 remote replay / debug 主链路的关键约束是：

- `rd.remote.connect` 返回的是 live `remote_id`，不是占位 handle。
- `rd.remote.ping` 用于确认该 `remote_id` 仍然连着 live endpoint。
- `rd.capture.open_replay` 需要通过 `options.remote_id` 显式进入 remote replay backend。
- remote `open_replay` 一旦成功，原 `remote_id` 会被对应 `session_id` 消费；如需新的 live handle，必须重新 `rd.remote.connect`。
- If a stale `remote_id` is reused, the expected lifecycle error code is `remote_handle_consumed`.
- Android remote 可通过 `rd.remote.connect` 的 `options.transport="adb_android"` 触发仓库内置的 `adb` bootstrap。
- 长链任务优先通过 `rd.session.get_context` / `rd.session.update_context` 维护当前 context，而不是依赖模型自己记住上一轮 handle 与 artifact 路径。
- `active_event_id` 与对外暴露的 canonical `event_id` 只表示可被 `rd.event.get_action_details` round-trip 的 action event；对 `rd.resource.get_usage` / `rd.resource.get_history` 中不可 round-trip 的底层记录，应查看 `raw_event_id` 与 `event_resolvable`。

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
- [docs/android-remote-cli-smoke-prompt.md](docs/android-remote-cli-smoke-prompt.md)：桌面 / Android 分层 smoke 与 contract 测试模板

## 关键约束

- tool catalog 的权威来源是 `spec/tool_catalog.json`。
- catalog 当前数量以 `tool_count` 字段为准；当前为 `198`，后续变更必须同步更新 validator、help 输出与文档口径。
- 运行时响应遵循共享契约；调试时优先检查 `ok`、`error_message`，必要时继续看 `error.details`。
- 默认参考根目录由 `rdx.bat` 或脚本自身位置推导；`RDX_TOOLS_ROOT` 仅用于覆盖默认值。
- `rd.event.set_active` 若收到不可解析的 `event_id`，必须失败且保持现有 runtime / context 状态不变。
- `rd.capture.close_file` 若目标 `capture_file_id` 仍被 live replay 持有，必须失败；推荐顺序是 `rd.capture.close_replay -> rd.capture.close_file`。

## 验证

```bat
python spec/validate_catalog.py
python cli/run_cli.py --help
python mcp/run_mcp.py --help
python scripts/check_markdown_health.py
```
