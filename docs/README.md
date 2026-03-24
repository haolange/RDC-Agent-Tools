# `rdx-tools` 文档导航

本目录记录独立分发的 `rdx-tools` 包文档。文档按“入口层 + 模型层 + 支撑层”组织，避免把平台真相与上层业务策略混在一起。

## 建议阅读顺序

1. [quickstart.md](quickstart.md)
   先跑通最短路径，确认 `CLI` / `MCP` 入口可用。
2. [session-model.md](session-model.md)
   理解 `.rdc`、`capture_file_id`、`session_id`、`context`、daemon 与 artifact 的关系。
3. [agent-model.md](agent-model.md)
   理解上层 Agent / framework 应如何安全使用 `rdx-tools`，以及哪些内容不属于仓库职责。
4. [doc-governance.md](doc-governance.md)
   理解功能改动如何映射到文档责任、量化门槛和提交流程。
5. [configuration.md](configuration.md)
   查看 runtime layout、环境变量、输出目录与根目录约束。
6. [troubleshooting.md](troubleshooting.md)
   排查启动失败、transport、参数位置、状态恢复等常见问题。
7. [tools.md](tools.md)
   查阅 tool catalog 的权威入口与校验命令。
8. [android-remote-cli-smoke-prompt.md](android-remote-cli-smoke-prompt.md)
   复用桌面 / Android 通用的分层 smoke / contract 执行 prompt。
9. [fixture-strategy.md](fixture-strategy.md)
   查看 first-party `.rdc` fixture 与测试分层策略。
10. [release-baseline.md](release-baseline.md)
   查看当前发布基线、已校正口径与已知发布阻塞。
11. [compatibility-notes.md](compatibility-notes.md)
   查看当前兼容边界、分发定位与上层接线约束。
12. [../scripts/README.md](../scripts/README.md)
   查看正式 `scripts/` 主链与治理规则，以及 `tool_contract_check.py` 的 remote 默认值 / 覆盖方式和 `release_gate.py --require-smoke-reports` 的当前真门禁口径。

## 文档边界

本目录负责：

- `CLI` / `MCP` / tool catalog / runtime 的平台说明。
- 最小可运行链路与状态模型。
- 常见失败恢复与维护规则。
- 文档自更新治理与功能变更到文档责任的映射。
- 可直接复用的少量执行模板文档。

本目录不负责：

- shader debug、reverse、analysis、optimize 等任务级策略。
- 上层 skills、system prompt、reference docs 的业务编排。
- 面向具体调试目标的固定 tool 序列。

## 关键入口

- `rdx.bat`
- `cli/run_cli.py`
- `mcp/run_mcp.py`
- `spec/tool_catalog.json`
- `scripts/check_markdown_health.py`
- `scripts/README.md`
