# 变更记录 `Changelog`

## 待发布 `Unreleased`

- 新增持久化 `context/session` state，支持本地 `.rdc` warm resume、多 session `current_session_id` 选择、trace-linked operation history、runtime metrics 与 bounded event-tree pagination。
- 新增 `rd.session.list_sessions`、`rd.session.select_session`、`rd.session.resume`、`rd.core.get_operation_history`、`rd.core.get_runtime_metrics`、`rd.core.list_tools`、`rd.core.search_tools` 与 `rd.core.get_tool_graph`。
- 新增只读 `rd.vfs.*` tools，以及配套的 CLI `vfs` commands，用于在 draws、passes、resources、pipeline、context 与 artifacts 上执行 JSON-first 导航。
- 修复 `scripts/release_gate.py`，使 `rg` fallback 能区分 literal/path scan 与 regex scan，并避免在无效 regex assembly 时崩溃。
- 新增 `pyproject.toml`，让依赖、pytest markers 与本地开发 entrypoints 可复现，同时不改变 repo-first runtime model。
- 新增聚焦 VFS 的 tests 与 release-gate regression coverage。
- 新增 stable/GA CLI surface：`--version`、`version --json`、`completion powershell|bash|zsh|fish`、自包含 Windows x64 release package、安装生命周期脚本、package verification 与 1.x 兼容承诺文档。
