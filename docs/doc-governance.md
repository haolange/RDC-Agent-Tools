# 文档治理

本文说明 `rdx-tools` 的文档如何随功能迭代保持自更新，同时继续围绕工具第一性组织内容。

本文不替代 [`AGENTS.md`](../AGENTS.md) 的硬约束；它解释为什么这些规则存在，以及功能变化应如何映射到文档责任。

## 1. 治理目标

文档治理的目标不是“自动生成文档”，而是让每次功能迭代都无法绕过文档责任。

文档必须持续回答这些第一性问题：

- 平台规范源是什么。
- `.rdc` 如何进入可操作 session。
- `context`、daemon、session state、artifact、context snapshot 如何协同。
- `remote_id` 的生命周期边界是什么。
- 上层 Agent / framework 的责任边界在哪里。

## 2. 分层职责表

- `README.md`
  - 仓库定位、规范源优先级、文档导航。
- `docs/quickstart.md`
  - 最短顺序链路。
- `docs/session-model.md`
  - 状态面、句柄生命周期、`.rdc` 到 session 的第一性模型。
- `docs/agent-model.md`
  - 上层 Agent 边界、恢复 ownership、平台使用原则。
- `docs/troubleshooting.md`
  - 常见故障、状态面差异、恢复路径。
- `docs/tools.md`
  - catalog 入口与规范源说明。
- `docs/doc-governance.md`
  - 功能变更到文档责任的映射、量化指标、提交流程。
- `docs/android-remote-cli-smoke-prompt.md`
  - 面向开发 Agent 的分层 smoke / contract 测试模板。
- `scripts/README.md`
  - 正式 `scripts/` 主链、分类与进入标准。

## 3. 功能变更到文档责任的映射矩阵

### 入口与帮助输出变更

如果改动影响 `CLI --help`、`MCP --help`、launcher 入口、transport、交互语义，至少检查：

- `README.md`
- `docs/quickstart.md`
- `docs/troubleshooting.md`
- `docs/doc-governance.md`

### remote endpoint / bootstrap 变更

如果改动影响 `rd.remote.connect`、`rd.remote.ping`、`options.remote_id`、Android `adb` bootstrap、remote endpoint 建连或清理语义，至少检查：

- `README.md`
- `docs/quickstart.md`
- `docs/session-model.md`
- `docs/agent-model.md`
- `docs/troubleshooting.md`
- `docs/tools.md`
- `docs/doc-governance.md`

### tool schema / contract / catalog 总量变更

如果改动影响 `rd.*` tool、tool 参数、tool 返回字段、能力边界、共享契约，或 catalog 当前总量，至少检查：

- `README.md`
- `docs/tools.md`
- `docs/agent-model.md`
- `docs/doc-governance.md`
- 如涉及开发后自测流程，再检查 `AGENTS.md`

### session / context / daemon 语义变更

如果改动影响 `.rdc -> session` 链路、`context`、daemon、session state、artifact、context snapshot、错误恢复路径，至少检查：

- `docs/session-model.md`
- `docs/quickstart.md`
- `docs/troubleshooting.md`
- `docs/agent-model.md`
- `docs/doc-governance.md`
- 如涉及开发 Agent 自测，再检查 `docs/android-remote-cli-smoke-prompt.md`

### 运行时前置条件 / 环境变量变更

如果改动影响 runtime layout、环境变量、根目录约束，至少检查：

- `README.md`
- `docs/configuration.md`
- `docs/troubleshooting.md`
- `docs/doc-governance.md`

### `scripts/` Governance

- `README.md`
- `docs/README.md`
- `docs/android-remote-cli-smoke-prompt.md`
- `docs/doc-governance.md`
- `docs/troubleshooting.md`
- `scripts/README.md`
- `AGENTS.md`

如果改动影响 release smoke 门禁、`rdx.bat` 非交互 launcher 入口，或 `tool_contract_check.py` / `smoke_report_aggregator.py` 的当前输出约定，也必须补一轮真实 local-only smoke 检查。

## 4. 文档口径规则

- 规范源优先级必须一致：catalog / contract 优先，runtime 其次，`CLI` 不是规范源。
- handle 必须写成运行时引用，不得暗示长期稳定。
- 示例默认按顺序执行语义编写。
- 未验证的行为不得写成“已验证”。
- 不把并发现象写成平台定义。
- 不把恢复 ownership 错放给仓库。
- 如果 `remote_id` 被写入文档，必须说明它在 remote `open_replay` 成功后会被消费。
- 如果 catalog 已公开 `rd.session.get_context` / `rd.session.update_context`，核心文档中必须能找到它们的角色说明。
- 如果文档写到 `event_id`，必须区分 canonical action event 与底层 `raw_event_id`；不得暗示任意 RenderDoc 原始 `eventId` 都能直接喂回 `rd.event.set_active`。

## 5. 量化指标

这些指标不需要做成仪表盘，但必须成为交付门槛：

- 变更触发率
  - 任何影响平台模型、入口、契约、状态语义的改动，文档检查率必须是 100%。
- 核心文档覆盖率
  - 每次平台改动必须检查全部核心文档；按需更新，但不可跳过检查。
- 结构校验通过率
  - `python scripts/check_markdown_health.py` 必须 100% 通过。
- 入口一致性校验
  - `python spec/validate_catalog.py`
  - `python cli/run_cli.py --help`
  - `python mcp/run_mcp.py --help`
  - 以上命令在相关改动下必须通过。
- 会话链路验证
  - 涉及 session、daemon、context 的改动，必须至少有 1 条顺序链路验证记录。
- 未验证声明
  - 若任一项无法验证，交付中必须明确列出未验证项、原因、风险。

## 6. 推荐提交流程

1. 先判断这次功能改动触发了哪类文档责任。
2. 再更新相关文档，而不是只改单一主文档。
3. 然后运行文档检查与入口检查：

```bat
python scripts/check_markdown_health.py
python spec/validate_catalog.py
python cli/run_cli.py --help
python mcp/run_mcp.py --help
```

4. 若改动影响 `.rdc` 会话链路，再顺序验证一次最小链路。
5. 若改动影响 remote / bootstrap / transport，再按需要参考 `docs/android-remote-cli-smoke-prompt.md` 组织更完整的 smoke / contract 流程。
6. 若改动影响本地 smoke / release gate，再补一轮真实 local-only smoke：

```bat
python scripts/rdx_bat_command_smoke.py
python scripts/tool_contract_check.py --local-rdc <external-rdc> --skip-remote --transport both
python scripts/smoke_report_aggregator.py --command-json intermediate/logs/rdx_bat_command_smoke.json --tool-json intermediate/logs/tool_contract_report.json --out intermediate/logs/rdx_smoke_issues_blockers.md
python scripts/release_gate.py --require-smoke-reports
```

7. 最后在交付说明中说明更新范围、无需更新项、验证范围与清理结果。

## 7. 自动检查与人工审阅的分工

- `scripts/check_markdown_health.py`
  - 负责结构性约束：编码、核心文档存在、核心互链、已知损坏文本模式与关键一致性断点。
- `AGENTS.md`
  - 负责硬约束：什么情况下必须更新文档、最低更新粒度、最低验证门槛，以及开发 Agent 自测引导。
- 人工审阅
  - 负责语义严谨度：第一性是否正确、边界是否写清、验证口径是否受控。

release gate 中的 `docs:governance-baseline` 代表文档治理基线是否通过，不只是 Markdown 编码或链接是否正确。
