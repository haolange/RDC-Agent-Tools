# `tests/fixtures/`

本目录保留 `rdx-tools` 的正式 first-party fixtures。

当前仓库还没有可公开分发的 `.rdc` 正式样本；这是 stable/GA 的剩余硬门禁。在补齐 sample capture 前：

- `unit` / `contract` 继续使用现有的 fake controller、mock payload 与 schema 校验。
- `fixture_integration` 和 `scripts/smoke_cli.sh` 可以通过显式参数接收外部样本。
- 一旦本目录出现 `*.rdc`，`scripts/smoke_cli.sh` 会默认使用第一个按名称排序的 fixture。
- `scripts/release_gate.py --require-smoke-reports` 会要求 bash smoke log 包含 `[smoke] PASS`。

后续一旦引入正式 `.rdc` fixture，必须同步更新：

- [README.md](../../README.md)
- [docs/quickstart.md](../../docs/quickstart.md)
- [docs/fixture-strategy.md](../../docs/fixture-strategy.md)
- [scripts/smoke_cli.sh](../../scripts/smoke_cli.sh)

正式 `.rdc` fixture 必须附带来源、生成方式、SHA256、大小说明，单个 fixture 目标小于 25 MB。
