# Agent 使用模型

本文面向上层 Agent / framework，说明 `rdx-tools` 应如何被安全使用，以及为什么“只给一份 tools 清单”通常不够。

上层应默认把本仓库理解为 `.rdc` 离线 replay / 调试 / 导出平台，而不是应用侧控制平台。

本文只描述平台约束，不描述具体业务策略。

## 1. 规范源优先级

对上层 Agent 来说，判断平台定义时应遵循以下顺序：

1. `spec/tool_catalog.json` 与共享响应契约
2. runtime 实际行为
3. `CLI` convenience wrapper（便捷封装）

因此：

- tool 能力面与参数语义，以 catalog 和共享契约为准。
- runtime 行为是平台真相的运行时体现。
- `CLI` 不是完整能力面的等价镜像，也不是规范源。

## 2. 为什么 tools 清单不够

仅有 `spec/tool_catalog.json` 或 tool 名称列表，通常仍不足以让 Agent 稳定工作，原因包括：

- 某些 tool 存在前置依赖，必须先建立 session。
- 关键状态对象需要跨步骤传递，例如 `capture_file_id`、`session_id`、`event_id`。
- `capture_file_id`、`session_id` 是运行时句柄，不是长期稳定标识。
- remote 路径还存在 `remote_id` consumed 生命周期，不能把它当作可无限复用的句柄。
- 长链任务如果没有 context snapshot，模型很容易忘记上一轮 focus 与 artifact 路径。
- 一个 context 现在可以持有多条本地 session 记录；如果 Agent 只记住单个 `session_id`，很容易把“当前选中 session”和“context 持有的全部 session”混为一谈。
- 不是所有底层 RenderDoc `eventId` 都能回灌到 `rd.event.*`；上层必须区分 canonical `event_id` 与 `raw_event_id`。
- 不要在 replay 仍存活时提前调用 `rd.capture.close_file`；推荐清理顺序是先 `rd.capture.close_replay`，再关闭对应 capture handle。
- 失败恢复依赖共享契约，调用端需要明确检查 `ok`、`error.message` 与 `error.details`。

因此，Agent 需要的不只是 catalog，还需要平台使用模型。

## 3. 仓库负责什么

本仓库负责：

- 提供 `CLI`、`MCP`、tool catalog 与 runtime 约束。
- 提供从 `.rdc` 到 session 的最小链路。
- 提供状态模型、错误面与失败恢复入口。

本仓库不负责：

- debug / analysis / reverse / optimize 的任务策略。
- 上层用户意图到 tool 序列的业务编排。
- 任务级 playbook 或专家 workflow。
- 自动恢复策略的最终决策。

## 4. Agent 的推荐使用原则

上层 Agent / framework 应遵循这些平台级原则：

- 先根据宿主条件选择入口，再决定具体 tool 序列。
  - 可直接访问本地进程、文件系统与 daemon 的宿主，默认 local-first，优先使用 daemon-backed `CLI`。
  - 只有宿主不能直达本地环境，或用户明确要求按 `MCP` 接入时，才走 `MCP`。
- 不论走 `CLI` 还是 `MCP`，任务开始时都应向用户说明当前采用的入口模式。
- 选择 `MCP` 前，先确认宿主已经配置对应 MCP server。
  - 如果未配置，必须显式阻断并提示配置，而不是假设工具可用。
- 先建立 session，再做 inspection。
  - 对 `.rdc` 的平台最小链路是 `rd.core.init -> rd.capture.open_file -> rd.capture.open_replay -> rd.replay.set_frame`。
- 对 remote 路径，先拿到 live `remote_id`。
  - 推荐链路是 `rd.remote.connect -> rd.remote.ping -> rd.capture.open_replay(options.remote_id=...)`。
  - remote `open_replay` 成功后，原 `remote_id` 会被 session 消费；不要继续对它执行 `ping` / `disconnect` / 再次 `open_replay`。
  - 如果复用了已经失效的 handle，预期错误码应为 `remote_handle_consumed`。
  - 对 Android remote，不要假设外部 `qrenderdoc` 已经替你做了 bootstrap；`rd.remote.connect` 的 `options.transport="adb_android"` 才是平台定义入口。
- 显式保存关键状态。
  - 至少保存 `capture_file_id`、`session_id`、当前 `frame_index`、必要时保存 `event_id`。
  - 长链任务优先通过 `rd.session.get_context` / `rd.session.list_sessions` / `rd.session.update_context` 维护 context，而不是依赖模型自己记住上一轮 handle 与 artifact 路径。
- 多 session context 下显式选择 current session。
  - 当 `rd.session.list_sessions` 返回多条记录时，后续 inspection 前优先通过 `rd.session.select_session` 锁定当前工作面。
- 对 daemon 重启后的本地链路，优先读取恢复面。
  - 本地 `.rdc` session 可通过 `rd.session.get_context` / `rd.session.resume` 自动或显式恢复。
  - remote session 不会自动重连；一旦 daemon 退出，应重新执行 `rd.remote.connect -> rd.remote.ping -> rd.capture.open_replay(options.remote_id=...)`。
- 先用 discovery 接口，再决定注入多少 tool 描述。
  - `rd.core.list_tools` 适合按 `namespace`、`group`、`capability`、`role` 做结构化枚举。
  - `rd.core.search_tools` 适合按当前任务关键词做轻量筛选。
  - `rd.core.get_tool_graph` 适合查看 prerequisite 与 macro-to-canonical 依赖图，而不是让模型自己猜工具调用链。
  - 默认推荐顺序应理解为：canonical `rd.*` 主接口 -> `rd.macro.*` -> `rd.session.*` / `rd.core.*` 元信息层 -> `rd.vfs.*` 导航层。
  - 只有当任务明确在问“怎么浏览”“有哪些路径可看”“怎么快速看结构”时，才应把 `rd.vfs.*` 提前。
  - `tabular/tsv projection` 只是结构化结果的表格化摘要，用于更快扫描与复制，不表示语义重要度排序。
- 只把可 round-trip 的 canonical `event_id` 当作后续 `rd.event.set_active` 候选。
  - `rd.resource.get_usage` / `rd.resource.get_history` 返回的 `raw_event_id` 仅用于诊断，不应默认直接传回 `rd.event.*`。
- 把 handle 当作短生命周期引用。
  - 上层如需缓存，必须准备重建 session 的恢复路径，而不是把 handle 当成永久主键。
- 先读 catalog 的 `prerequisites`，再决定 tool 序列。
  - session / capture / remote / capability 前置应优先做静态满足性判断。
- 显式参数优先于 snapshot 默认值。
  - `rd.session.*` 只用于补充上下文，不应覆盖本轮调用显式给出的参数。
- 优先轻量调用。
  - 先获取事件、状态、元数据，再进入导出、diff、debug 等更重的操作。
- 失败时先看共享契约。
- 先检查 `ok` 与 `error.message`。
- 如果需要归因，再看 `error.details.source_layer`、`classification`、`capture_context`、`renderdoc_status`。
- 对长耗时操作，优先看 progress/status，而不是把“静默等待”当作失败信号。
- 对最近动作与恢复尝试，优先看结构化历史而不是日志猜测。
  - `rd.core.get_operation_history` 返回 trace-linked 最近调用。
  - `rd.core.get_runtime_metrics` 返回恢复次数、拒绝次数、进程内存与近期耗时摘要。

## 5. 恢复职责 ownership

仓库负责提供标准错误面与平台约束；上层 Agent / framework 负责决定：

- 是否重试
- 是否重建 session
- 是否切换 context
- 是否降级任务目标

仓库不承诺自动恢复策略，也不提供任务级恢复 playbook。

## 6. `CLI`、daemon 与 `MCP` 的职责

这三者不是同一层概念：

- `CLI`
  - daemon-backed 本地命令入口。
  - 适用于人工、脚本、CI、本地 Agent。
- daemon
  - 长生命周期 runtime / context 持有层。
  - 是唯一的 live runtime / context owner，用于跨命令、跨轮次复用 live session、focus、recent artifacts 与 runtime owner。
  - 不是 `MCP` 的附属概念；`CLI` 与 `MCP` 都依赖同一套 daemon / context 机制。
- `MCP`
  - 协议桥接入口。
  - 适用于无法直接进入本地环境的外部宿主，或用户明确要求按 `MCP` 接入的场景。
  - 提供 tool discovery、schema 化调用与外部 host 集成。

因此，不应把“是否是 Agent”当成 `CLI` 与 `MCP` 的分界线。真正的分界线是：

- 调用方能否直接进入本地环境。
- 调用方应使用哪种 daemon-backed adapter。

## 7. 建议上层文档说明到什么程度

上层框架文档建议描述：

- 宿主如何选择 `CLI`、daemon 与 `MCP`
- 任务开始时如何声明当前入口模式
- 选择 `MCP` 时如何校验 MCP server 已配置
- 如何建立与复用 session
- 如何在一个 context 内枚举并切换多条 session
- daemon 停止后哪些状态会保留，哪些 remote 链路需要重建
- 如何保存关键状态
- 如何通过 `rd.session.get_context` / `rd.session.update_context` 维护长链状态
- 如何通过 `rd.core.list_tools` / `rd.core.search_tools` / `rd.core.get_tool_graph` 控制 tool discovery 的粒度
- 如何处理失败与恢复

上层框架文档不必描述：

- 每种业务目标的固定 tool 顺序
- 每一种渲染问题的专家级分析步骤

换句话说，上层文档应提供“护栏”和“原则”，而不是替 Agent 完成全部思考。
