# 兼容性说明

本文说明 `rdx-tools` 当前公开发布时的兼容边界与推荐使用方式。

## 运行时兼容边界

- 当前主支持运行时：`Windows`
- 当前主入口：
  - `rdx.bat`
  - `cli/run_cli.py`
  - `mcp/run_mcp.py`
- `CLI` 与 `MCP` 都是 daemon-backed adapter；当前不再公开 direct runtime 执行模式。
- `spec/tool_catalog.json` 当前公开的规范 `rd.*` tools 数量以 `tool_count` 字段为准。
- `rd.vfs.*` 当前属于 read-only、JSON-first 导航层；它适合探索与浏览结构，不保证覆盖全部精细控制语义；需要精确分析时应回到 canonical `rd.*`。
- `tabular/tsv projection` 只覆盖声明支持的摘要场景；它是表格化展示投影，用于提升扫描效率，不表示语义重要度排序。
- daemon 停止后会保留本地 `.rdc` 的持久化恢复索引；remote session 仍需要显式重建。

## 分发与安装定位

- 当前首发定位不是“包管理器优先”的跨平台产品。
- `pyproject.toml` 的目标是：
  - 统一依赖
  - 统一本地开发环境
  - 统一 pytest 配置
- 当前官方使用方式仍是：
  - 直接在仓库根运行
  - 或显式设置 `RDX_TOOLS_ROOT`

## 与上层宿主 / framework 的边界

- `rdx-tools` 只提供平台真相，不承诺上层 framework 自动装配。
- 如果上层宿主采用源码 + 文档手工接线模式：
  - 应显式校验 `tools_root`
  - 应显式验证 required paths
  - 应按兼容矩阵选择对应版本

## 当前已知限制

- 缺少 first-party `.rdc` 正式 fixture时，`tool_contract_check.py` 仍需要外部样本。
- `release_gate.py` 默认校验 clean checkout 的结构 / 入口 / 文档基线；正式发版前如果要把 smoke 纳入门禁，应显式提供样本并启用 `--require-smoke-reports`，或先把 first-party fixture 纳入仓库。
- GPU / remote / Android 路径仍受真实环境限制，不应被文档误写成“默认可用”。
- 只读 `rd.vfs.*` 不承诺完全复制 shell/VFS 产品的全部人机交互语义；其目标是给图形工程师与 Agent 提供统一的结构化导航面，而不是成为正式调试入口。
