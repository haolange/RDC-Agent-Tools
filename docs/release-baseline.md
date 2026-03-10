# 发布基线（2026-03-10）

本文用于替代 2026-03-09 之前若干研究报告中的过期口径，明确 `rdx-tools` 当前的真实发布基线。

## 已校正结论

- `rdx-tools` 不是“`pytest` 不可用”或“几乎无行为测试”。
  当前仓库已经包含 `release_gate`、CLI/VFS、daemon/context、tool contract、event/pipeline/resource 等测试面。
- `release_gate.py` 的问题依然真实存在，但根因不是“完全没有 fallback”，而是：
  - `rg` 不可用/权限失败时需要 Python fallback；
  - literal/path 规则与 regex 规则必须分开处理；
  - release gate 不应把自己的 rule 定义与测试样例误判成仓库泄漏。
- 当前 catalog 数量以 `spec/tool_catalog.json` 为准；截至 2026-03-10 为 `202`。
- 只读 `rd.vfs.*` 已进入公开能力面，但它是导航层，不是第二套平台真相。

## 当前产品边界

- `rdx-tools` 是平台真相与运行时：
  - `CLI`
  - `MCP`
  - `daemon/context`
  - `spec/tool_catalog.json`
  - shared response contract
- `rdx-tools` 不是上层 workflow / framework 仓库。
- 当前官方分发定位仍是：
  - 仓库优先
  - Windows 主运行时
  - 源码 + 文档接线

## 当前发布阻塞

- 缺少 first-party `.rdc` 正式 fixture，因此 local-only smoke 仍需要显式传入外部样本；当前闭环依赖注入式样本，而不是仓库内置 fixture。
- `release_gate.py` 现在应当做到：
  - 干净 checkout 下可完成结构 / 入口 / 文档 / manifest 门禁；
  - 若仓库内已经存在 first-party `.rdc` fixture，或显式使用 `--require-smoke-reports`，则必须补齐当前 smoke 报告与 truth JSON；
  - `--require-smoke-reports` 不是“只看文件存在”，而是要读取当前 command / MCP / daemon smoke truth，任一 blocker / fatal error 都必须失败；
  - 已有部分 smoke 报告但未完成整套输出时，必须明确失败而不是静默放过。
- `tool_contract_check.py --local-rdc <path> --skip-remote --transport both` 必须能在真实 local 样本上跑到 MCP / daemon `blocker = 0`；否则 local-only 发布闭环仍未完成。
- `rd.vfs.*` 已进入 catalog，但仍需持续通过 contract / transport 验证，防止仅文档公开、运行时漂移。

## 面向用户的稳定承诺

- `spec/tool_catalog.json` 是规范源。
- `rd.vfs.*` 是只读探索层：
  - 负责导航、解析、读取。
  - 不负责修改 runtime、切换 event、导出资源或更新 context。
- 上层如需做真正的状态修改、导出、切换与恢复，仍应调用原有 canonical `rd.*` tools。
