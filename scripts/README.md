# `scripts/` 治理与正式主链

本文说明 `rdx-tools` 仓库中 `scripts/` 目录的正式脚本集合、分类边界与进入标准。

## 正式脚本分类

### 运行时启动器

- `rdx_bat_launcher.ps1`
  - `rdx.bat` 背后的 runtime launcher，不作为独立治理脚本使用。

### 文档 / Release Gate

- `check_markdown_health.py`
  - 文档编码、互链与治理基线检查。
- `release_gate.py`
  - 发布前结构、入口、报告与 manifest 门禁检查。
  - 默认用于 clean checkout 结构门禁；若传入 `--require-smoke-reports`，或仓库内已有 first-party `.rdc` fixture，则 smoke 报告变为必需项。
- `generate_release_checksums.py`
  - 为源码 release 资产生成 `sha256` 校验文件。

### Smoke / Contract 检查

- `rdx_bat_command_smoke.py`
  - `rdx.bat` 入口 smoke。
- `tool_contract_check.py`
  - catalog 全量 `rd.*` tools contract / transport 检查。
- `smoke_report_aggregator.py`
  - 聚合 blockers / detailed 汇总报告。

### 维护脚本

- `package_runtime.py`
  - 复制 runtime staging 内容并生成 manifest。
- `cleanup_workspace.py`
  - 只清理仓库根目录内的忽略产物与临时目录。

## 进入标准

新脚本只有满足以下条件时，才可以进入 `scripts/` 正式主链：

- 所有路径解析都以 `rdx.bat` 所在根目录为基准。
- 不得硬编码个人机器路径、个人 `adb.exe`、个人设备 serial、桌面样本路径或个人调试目录。
- 必须说明脚本归属的分类：runtime launcher、docs / gate、smoke / contract、maintenance 之一。
- 如果脚本是正式入口，必须补测试，并在必要时补文档引用。
- 如果脚本改变了正式脚本集合、门禁链或 smoke / contract 用法，必须同步更新 `README.md`、`docs/README.md`、`docs/doc-governance.md`、`docs/troubleshooting.md` 与 `AGENTS.md`。

## 禁止项

以下内容不得以正式脚本形式留在主仓库：

- 单次问题排障脚本。
- 单样本专项调查脚本。
- 面向某台个人设备的临时脚本。
- 一次性复盘 / 报告生成脚本。
- 参数实验脚本。

如果确实需要做一次性调查，应在任务线程内临时组织，不应把该类脚本沉淀为仓库正式接口。
