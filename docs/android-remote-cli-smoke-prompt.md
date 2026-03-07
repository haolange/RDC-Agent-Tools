# 桌面与 Android 通用的分层 Smoke / Contract 测试 Prompt

?????????????? prompt??????? Agent ? `rdx-tools` ??????????????????????? / remote / `MCP` / daemon / ?? catalog tools contract???????????????????????????

注意：

- 本文不是平台规范源，也不替代 `spec/tool_catalog_196.json`、共享契约或 runtime 实际行为。
- 本文的目标是把测试做成“可分层定位、可迭代修复、可持续复测”的流程，而不是一次性跑完脚本后只得到一个模糊结论。
- 若 prompt 与平台真实行为冲突，应以 catalog / contract / runtime 为准。

## 适用场景

- 目标是验证 `rdx-tools` 在桌面环境与 Android remote 环境下的真实可用性。
- ?????? remote?????? session?remote session?`CLI`?`MCP`?daemon??? catalog ???? `rd.*` tools?
- 目标是每次测试后都留下“问题报告 + 证据 + 分层归因 + 后续修复建议”，方便下一个 Agent 接力修复，直到结果收敛到理想状态。

## 可复用 Prompt

```text
在仓库根目录 `<repo_root>` 工作。

目标是对本库 `rdx-tools` 做一次“桌面 + Android remote 通用”的分层 smoke / contract 测试。测试样本、目标 transport、输出路径、context 名、报告路径、artifact 路径都优先使用用户显式提供的值；如果用户没有给出这些输入，先向用户确认，再继续执行。不要修改代码；本次任务以测试、观测、记录、清理、报告为主。遵守仓库 `AGENTS.md`：路径以 `rdx.bat` 所在目录为根，不依赖父目录逃逸；测试后必须清理 daemon / 残留进程 / 临时资源，并在交付说明里写明清理结果。

本次测试不要只依赖现有脚本。应采用“分层主判据”策略：
- 第一主判据：`CLI` 直接调用的最小链路是否可运行。
- 第二主判据：local session、remote session、daemon / `MCP` transport 是否分别可运行。
- ???????? catalog tools contract ????????????????
- 现有大脚本可以作为补充证据，但不能覆盖前面已经得到的更直接、更短链路的事实结论。

执行要求如下：

1. 先做 fail-fast 前置检查，并记录基线。
- 记录：
  - `adb devices -l`
  - `adb forward --list`
  - 当前 `python.exe` 进程快照
- 如果本轮任务要求包含 Android remote：
  - 若没有可用 Android 设备 serial，或设备状态是 `offline` / `unauthorized`，立即停止 Android remote 后续测试，并输出 blocker 报告，说明“当前 shell 未看到可用 Android 设备”。
  - 这类 blocker 只阻断 Android remote 分支；不要因此跳过桌面 local / 其他可执行分支，除非用户明确要求整轮停止。
- 不要手工执行 `adb forward tcp:38920 tcp:38920`，也不要把某个固定 host:port 是否监听当作通用前置检查。
- Android remote 的 bootstrap 必须通过仓库自身 `rd.remote.connect(options.transport="adb_android")` 完成。

2. 做仓库基础可运行性检查。
- 顺序运行：
  - `python spec/validate_catalog.py`
  - `python cli/run_cli.py --help`
  - `python mcp/run_mcp.py --help`
  - `python mcp/run_mcp.py --ensure-env --daemon-context <test_context>`
- 任一失败都记录为 blocker。
- 如果这些基础入口失败，后续只保留能继续收集证据的最小验证与清理，不要盲跑更长链路。

3. 按复杂度分层执行测试，不要跳层。

### Level 0: 环境与入口层
- 目标：确认仓库入口、catalog、runtime layout 是否可用。
- 必须覆盖：
  - `validate_catalog.py`
  - `cli --help`
  - `mcp --help`
  - `mcp --ensure-env`
- 这一层失败时，要明确归因是环境 / 入口 / 布局问题，而不是 tool 逻辑问题。

### Level 1: 本地 local 最小链路
- 目标：确认桌面 local session 能建立。
- 使用 `CLI` 或等价最短路径，优先验证：
  - `rd.core.init`
  - `rd.capture.open_file`
  - `rd.capture.open_replay`
  - `rd.replay.set_frame`
  - `rd.event.get_actions`
- 这一层使用的样本文件必须来自用户显式提供；如果用户没有提供，先向用户确认，不要在 prompt 里写死某个文件路径。
- 若 local replay 打不开，要将问题归类为：
  - 样本损坏 / 不兼容
  - 本地 replay runtime 问题
  - 当前证据不足

### Level 2: Remote 最小链路
- 目标：确认 remote endpoint 是否真的建起来，并能进入 remote replay。
- 使用独立 daemon context，context 名由本轮任务显式指定，且应避免与已有 context 冲突。
- 先显式启动 daemon：
  - `python cli/run_cli.py --daemon-context <remote_test_context> daemon start`
- 然后按顺序调用以下 tool，全部通过 `python cli/run_cli.py --daemon-context <remote_test_context> call ... --json --connect` 执行，并保存每一步原始 JSON 输出摘要：
  - `rd.core.init`
  - `rd.remote.connect`
  - `rd.remote.ping`
  - `rd.capture.open_file`
  - `rd.capture.open_replay`
  - `rd.replay.set_frame`
- `rd.remote.connect` 的参数必须以本轮任务和平台定义为准：
  - 如果测试的是 Android remote，则必须显式传 `options.transport="adb_android"` 与 `options.device_serial="<adb devices -l 里看到的目标 serial>"`
  - 如果用户已显式给出 `host`、`port`、`timeout_ms`，则按用户要求传
  - 如果用户没有给出这些值，则基于当前平台文档、工具契约和本轮测试目标确定，并在报告中写明实际使用值
- 不要让 PowerShell 破坏 JSON 参数；必要时可用内联 Python、临时变量或其他可靠方法传入合法 JSON，但不要改仓库代码。
- `rd.remote.connect` 成功后，必须额外记录：
  - 返回的 `remote_id`
  - `detail.endpoint`
  - `detail.bootstrap` 中所有与本轮连接有关的关键字段
  - `adb forward --list` 在 connect 之后的实际变化
- 如果 `rd.remote.connect` 失败，判定为 remote bootstrap / remote endpoint blocker，停止该 remote 长链路，但保留已有证据和命令输出摘要。
- 如果 `rd.remote.connect`、`rd.remote.ping` 成功，而 `rd.capture.open_replay(options.remote_id=...)` 失败，要明确区分并讨论以下几类可能性：
  - remote transport / runtime 问题
  - 样本 API 不受远端设备支持
  - 样本 GPU / extension 兼容性问题
  - 当前证据不足，暂不能精确归类
- 不要把 `capture open --connect` 作为 remote 是否可用的主判据，因为它会混入本地 replay / 样本兼容性因素。

### Level 3: 代表性 transport / workflow 层
- 目标：确认不是只有最短路径通，而是主要 transport 和几条代表性工具链也通。
- 至少分开验证：
  - local daemon 路径
  - remote daemon 路径
  - local `MCP` 路径
  - remote `MCP` 路径
- 至少要覆盖一组代表性工具链，例如：
  - core / capture / replay
  - event / pipeline / resource
  - texture / buffer / export
  - `rd.remote.*`
- 如果这一层失败，必须单独归类为：
  - `MCP` transport 问题
  - daemon transport 问题
  - local only 问题
  - remote only 问题
  - harness / 脚本执行问题

### Level 4: ?? catalog tools contract ?
- ???? `spec/tool_catalog_196.json` ??? catalog `rd.*` tools ?????????????????
- 可以运行补充脚本，例如：
  - `python scripts/tool_contract_check.py --local-rdc "<local_sample>" --remote-rdc "<remote_sample_or_same_file>" --transport <mcp|daemon|both> --artifact-dir "<artifact_dir>" --out-json "<out_json>" --out-md "<out_md>" --daemon-context-prefix "<context_prefix>"`
- 不要优先使用 `scripts/run_smoke_196_dual_sample.py`，除非本轮任务明确要求。
- 如果脚本支持环境变量控制 remote connect 参数，则在 remote 分支运行前设置与本轮任务一致的值，例如：
  - `RDX_REMOTE_CONNECT_TRANSPORT=<remote_transport>`
  - `RDX_REMOTE_DEVICE_SERIAL=<target_serial>`
- 如当前只有一个 `.rdc`，则允许 `--local-rdc` 与 `--remote-rdc` 指向同一文件，仅用于满足脚本接口；但报告中必须写明这是因为用户或本轮任务只提供了一个样本。
- ????????????????????? catalog ?? tools ???????
  - `pass`
  - `issue`
  - `blocker`
  - `scope_skip`
- 对所有 `rd.remote.*` 工具，必须单独抽一段汇总，不允许混在总体数字里一句带过。

4. 每轮测试都必须输出问题报告，方便后续 Agent 继续修复。
- 报告不是可选项，必须生成。
- 报告至少要包含：
  - 本轮测试范围与层级覆盖情况
  - 使用的样本与运行环境
  - 哪些层通过、哪些层失败
  - 每个 blocker / issue 的摘要
  - 每个问题的证据：原始命令、关键 JSON、关键错误消息、关键状态观察
  - 每个问题的分层归因：
    - 环境问题
    - 设备问题
    - remote bootstrap / remote endpoint 问题
    - local replay/runtime 问题
    - remote replay/runtime 问题
    - 样本 API 不支持问题
    - 样本 GPU / extension 兼容性问题
    - `MCP` transport 问题
    - daemon transport 问题
    - 工具契约 / harness 问题
    - 当前证据不足
  - 每个问题的“下一步修复建议”，要求能直接指导后续 Agent 继续工作
  - 若问题被判定为 blocker，要说明它阻断了哪些后续层级
  - 若某层被跳过，要明确写成 `scope_skip`，并解释原因
- 报告必须能支持“下一轮继续修复并复测”，不能只写笼统结论如“远程有问题”或“脚本失败”。

5. 最终交付结论要求。
- 最终结论必须按层汇报，而不是只给一个总成败。
- 至少要包含：
  - 设备是否被 `adb` 识别
  - local 最小链路是否成功
  - remote endpoint 是否成功建立
  - `rd.remote.connect -> rd.remote.ping -> rd.capture.open_replay -> rd.replay.set_frame` 是否成功
  - `MCP` 与 daemon 两条 transport 的 summary
- catalog tools ??? summary
  - 所有 `rd.remote.*` 工具的结果摘要
  - blocker / issue / scope_skip 数量
  - 如果失败，失败更像哪一层，以及哪些层已经被排除
- 如果生成了 JSON / MD 报告，必须引用路径；输出文件名、artifact 目录、context prefix 应使用本轮任务显式值或本轮新生成的唯一值，并在报告里写明。

6. 清理要求。
- 停止本次所有 daemon context，包括手工验证用 context 和补充脚本生成的 context。
- 清理本次测试产生的残留 Python 子进程（如果有）。
- 清理本次 `rd.remote.connect` 自动创建的 `adb forward`，仅清理本次新增的条目，不动测试前已存在的条目，也不动既有 `qrenderdoc` 条目。
- 清理本次临时 artifact，只清理本次新建内容，不动仓库既有产物。
- 若某个残留在第一次清理后仍存在，要再做一次终态确认，并在交付里说明是否最终清干净。
- 在最终说明最后单独写一句：`已清理` 或 `未完全清理（含原因）`。

????????? blocker???????????????????????????????????????? Agent ????????????????????????? catalog tools contract ?????????
```

## 使用说明

- 推荐把本文作为“执行模板”直接交给人工或 Agent。
- ????? / ????????? `CLI` ????????????runtime ??????? catalog tools ??? contract ???
- 这份 prompt 的重点不是一次性跑出一个大报告，而是把测试分层、把问题写清楚、让后续修复有可执行入口。
- 如果用户没有给出样本文件、目标 transport、输出路径、context 命名规则等输入，应先向用户确认，不要擅自硬编码。
- 如果后续仓库对 bootstrap、`remote_id`、`options.remote_id`、transport、tool 覆盖面、或清理语义有变更，应同步检查本文是否仍然匹配。
