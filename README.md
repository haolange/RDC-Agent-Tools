# `rdx-tools`

`rdx-tools` 是面向 `RenderDoc` 的本地工具集，用于暴露稳定的 `rd.*` tool 能力、管理 `.rdc` 到 replay session 的运行时链路，并为本地直接执行与协议桥接提供统一入口。

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

统一 launcher，用于启动本地入口或协议桥接入口。

- 默认模式：交互式入口，提供 `Start CLI`、`Start MCP`、`Help`。
- `--non-interactive`：脚本与自动化入口。

```bat
rdx.bat
rdx.bat --non-interactive cli --help
rdx.bat --non-interactive mcp --ensure-env
```

### `cli/run_cli.py`

本地直接执行入口，适合人工、脚本、CI 和可直接访问本地环境的 Agent。

- 面向“命令”而不是 tool schema。
- 负责把常见平台动作封装成 `capture open`、`capture status`、`daemon status` 等命令。
- 可直接在当前进程执行，也可通过 `--connect` 复用 daemon / context。

### `mcp/run_mcp.py`

`MCP` server 入口，适合无法直接进入本地环境的外部宿主，或用户明确要求按 `MCP` 接入的场景。

- 暴露 catalog 当前定义的全部 `rd.*` tools；当前 `tool_count` 为 `202`。
- 新增只读 `rd.vfs.*` 入口，用于以 JSON-first 方式探索 draw/pass/resource/pipeline/context 结构。
- 已公开包含 `rd.session.get_context` 与 `rd.session.update_context`。
- catalog 现已为 tool 提供结构化 `prerequisites`，上层 Agent 应在调用前优先做静态前置检查，而不是依赖试错。
- 由上层 client 进行 tool discovery、参数组织与调用编排。

## 入口选择原则

选择入口时，按以下顺序判断：

1. 调用方能否直接访问本地进程、文件系统与 daemon。
2. 如果能，默认 local-first，优先使用 `CLI` 或直接本地 runtime。
3. 如果任务需要跨多轮保活 live runtime / context，再显式依赖 daemon。
4. 只有调用方不能直达本地环境，或用户明确要求按 `MCP` 接入时，才使用 `MCP`。

补充约束：

- `daemon` 是长生命周期 runtime / context 持有层，不是 `MCP` 的附属概念。
- 不论走 `CLI` 还是 `MCP`，上层 Agent 都应先向用户说明当前采用的入口模式。
- 如果选择 `MCP`，但宿主没有配置对应 MCP server，必须显式报错或阻断，而不是假设平台能力已经存在。

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
- 如果复用了已经失效的 `remote_id`，预期生命周期错误码应为 `remote_handle_consumed`。
- Android remote 可通过 `rd.remote.connect` 的 `options.transport="adb_android"` 触发仓库内置的 `adb` bootstrap。
- 长链任务优先通过 `rd.session.get_context` / `rd.session.update_context` 维护当前 context，而不是依赖模型自己记住上一轮 handle 与 artifact 路径。
- `rd.remote.connect` 与 `rd.capture.open_replay` 在 daemon / streamable transports 下会更新结构化 progress；如宿主不支持 push，至少应通过 `daemon status` 读取 `active_operation`。
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
- [docs/fixture-strategy.md](docs/fixture-strategy.md)：first-party `.rdc` fixture 与测试分层策略
- [docs/release-baseline.md](docs/release-baseline.md)：当前发布基线与已校正口径
- [docs/compatibility-notes.md](docs/compatibility-notes.md)：当前兼容边界与分发定位
- [scripts/README.md](scripts/README.md)：正式 `scripts/` 主链与治理规则
- [CHANGELOG.md](CHANGELOG.md)：最近一轮发布级变更摘要

## `scripts/` 说明

正式支持的脚本主链见 [scripts/README.md](scripts/README.md)。
一次性调查脚本不属于受支持的仓库接口。

若需要把真实 local smoke 纳入发布门禁：

- 继续通过显式参数把外部 `.rdc` 样本传给 `tool_contract_check.py`
- 生成当前 `rdx_bat_command_smoke.*`、`tool_contract_report.*`、`rdx_smoke_issues_blockers.md`、`rdx_smoke_detailed_report.md`
- 再执行 `python scripts/release_gate.py --require-smoke-reports`

此时 `release_gate.py` 不再只看报告文件是否存在，而会读取当前 smoke truth JSON，确认 command / MCP / daemon 都没有 blocker 或 fatal error。

## 关键约束

- tool catalog 的权威来源是 `spec/tool_catalog.json`。
- catalog 当前数量以 `tool_count` 字段为准；当前为 `202`，后续变更必须同步更新 validator、help 输出与文档口径。
- 运行时响应遵循共享契约；调试时优先检查 `ok`、`error_message`，必要时继续看 `error.details`。
- 默认参考根目录由 `rdx.bat` 或脚本自身位置推导；`RDX_TOOLS_ROOT` 仅用于覆盖默认值。
- `rd.event.set_active` 若收到不可解析的 `event_id`，必须失败且保持现有 runtime / context 状态不变。
- `rd.capture.close_file` 若目标 `capture_file_id` 仍被 live replay 持有，必须失败；推荐顺序是 `rd.capture.close_replay -> rd.capture.close_file`。
- `rd.vfs.*` 是只读探索层，不替代结构化 `rd.*` canonical tools；所有修改、切换、导出与 context 更新仍继续通过原有 `rd.*` API 完成。

## 验证

```bat
python spec/validate_catalog.py
python cli/run_cli.py --help
python mcp/run_mcp.py --help
python scripts/check_markdown_health.py
```
