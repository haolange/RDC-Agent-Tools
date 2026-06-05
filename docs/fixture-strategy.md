# first-party fixture 策略

本文说明 `rdx-tools` 如何为 contract / integration / live smoke 测试准备 first-party capture fixtures。

## 目标

- 让 VFS 探索链路、bash CLI smoke 与关键 `rd.*` tools 能基于仓库自己控制的样本持续验证。
- 避免把第三方私有 capture、个人机器路径或不可再生样本当成正式测试依赖。
- 明确区分：
  - `unit`
  - `contract`
  - `fixture_integration`
  - `gpu_live`

## 推荐做法

### 1. 优先 first-party 最小 capture

- 通过最小 sample app 或可重复生成脚本生成 `.rdc`。
- 样本应覆盖至少一条稳定的 draw/pipeline/resource 链路。
- 优先保证：
  - 可公开分发
  - 可重复生成
  - 结果稳定

### 2. 仓库内只保留“正式 fixture”

- 正式 fixture 应放在 `tests/fixtures/` 或由固定脚本生成到该目录。
- 一次性调查样本、私人项目 capture、客户样本不进入仓库正式测试面。

### 3. 测试分层

- `unit`
  - 不依赖 `.rdc`，主要验证参数解析、fallback、path mapping、契约形状。
- `contract`
  - 允许使用伪造 controller / payload，但必须对齐 catalog 和 canonical contract。
- `fixture_integration`
  - 依赖 first-party `.rdc`，验证 open replay、VFS、pipeline/resource 查询等离线链路。
- `gpu_live`
  - 依赖真实 RenderDoc / GPU / remote endpoint，不作为默认本地门禁。

## 当前状态

- 当前仓库已具备 `contract` / `unit` 层能力。
- 当前 local-only 真样本闭环仍可采用“显式传入外部 `.rdc`”的方式，不把外部绝对路径沉淀进仓库。
- Stable/GA 必须补齐 first-party `.rdc` fixture；`scripts/smoke_cli.sh` 已支持默认发现 `tests/fixtures/*.rdc`。

## 接入要求

- fixture 变更必须同步更新：
  - `README.md`
  - `docs/quickstart.md`
  - `docs/troubleshooting.md`
  - `scripts/smoke_cli.sh`
- 一旦仓库内引入正式 `.rdc` fixture，`scripts/release_gate.py` 的 bash smoke 日志检查应自动转为必需门禁；在此之前，clean checkout 只要求结构 / 文档 / 入口门禁通过。
- 在未引入仓库内 fixture 之前，若显式使用 `python scripts/release_gate.py --require-smoke-reports`，则必须先运行 `bash scripts/smoke_cli.sh --rdc <sample.rdc>` 生成带 `[smoke] PASS` 的 `intermediate/logs/smoke_cli.log`。
- 所有正式 fixture 都必须说明来源、生成方式与适用测试层级。

English summary: release smoke evidence is optional in a clean checkout until a first-party `tests/fixtures/*.rdc` exists. If `--require-smoke-reports` is passed, maintainers must first run `bash scripts/smoke_cli.sh --rdc <sample.rdc>` and produce `intermediate/logs/smoke_cli.log` containing `[smoke] PASS`.
