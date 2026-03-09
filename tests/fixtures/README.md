# `tests/fixtures/`

本目录保留 `rdx-tools` 的正式 first-party fixtures。

当前仓库还没有可公开分发的 `.rdc` 正式样本；在补齐 sample capture 或 fixture 生成脚本前：

- `unit` / `contract` 继续使用现有的 fake controller、mock payload 与 schema 校验。
- `fixture_integration` 和 `tool_contract_check.py` 应通过显式参数接收外部样本，而不是假定仓库已经内置样本。

后续一旦引入正式 `.rdc` fixture，必须同步更新：

- [README.md](../../README.md)
- [docs/quickstart.md](../../docs/quickstart.md)
- [docs/fixture-strategy.md](../../docs/fixture-strategy.md)
- [scripts/tool_contract_check.py](../../scripts/tool_contract_check.py)
