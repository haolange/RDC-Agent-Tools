# Session 模型

本文说明 `rdx-tools` 的平台使用模型：怎样把一份 `.rdc` 变成可操作的 session，以及 `context`、daemon、session state、artifact、context snapshot 分别承担什么职责。

本文不讨论上层业务 workflow。shader debug、reverse、analysis、optimize 等任务策略应由上层 skills、system prompt、reference docs 决定。

## 1. 最小对象链路

一份 `.rdc` 进入 `rdx-tools` 后，典型平台链路是：

```text
.rdc
-> capture_file_id
-> session_id
-> frame_index / active_event_id
-> inspection / export / diff / assert
```

remote replay / debug 时，会额外经过：

```text
remote endpoint
-> remote_id
-> rd.capture.open_replay(options.remote_id)
-> remote session_id
```

各对象职责如下：

- `.rdc`
  - 原始 capture 文件。
- `capture_file_id`
  - `rd.capture.open_file` 返回的运行时文件句柄。
  - 当某个 replay 仍引用它时，平台不会允许 `rd.capture.close_file` 提前释放该 handle。
  - 表示 capture 文件已被 runtime 接管，但还未形成 replay session。
  - 它是运行时句柄，不是永久 ID，也不应默认作为跨重启、跨进程、跨环境、跨机器的长期缓存键。
- `session_id`
  - `rd.capture.open_replay` 返回的 replay session 句柄。
  - 大多数 inspection、navigation、export 类 `rd.*` tools 都依赖它。
  - 它同样是运行时句柄，可在同一条平台链路中跨步骤复用，但不应被视为长期稳定标识。
- `remote_id`
  - `rd.remote.connect` 返回的 live remote endpoint 句柄。
  - 它表示 runtime 已经建立远程连接，而不是“只保存 host/port 的占位引用”。
  - `rd.capture.open_replay` 若要进入 remote backend，必须通过 `options.remote_id` 显式引用它。
  - 一旦 remote `open_replay` 成功，该 `remote_id` 会被对应 `session_id` 消费；之后它不再是 live handle，如需新的 remote handle，必须重新 `rd.remote.connect`。
  - 如果复用了已经失效的 `remote_id`，预期生命周期错误码应为 `remote_handle_consumed`。
  - 它同样是运行时句柄，不应被视为长期稳定标识。
- `frame_index`
  - 当前 replay 所选帧。
- `active_event_id`
  - 当前焦点 action event，常作为后续 event 级分析的起点。
  - 只有可被 `rd.event.get_action_details` round-trip 的 event 才会被写入这里；`rd.event.set_active` 对不可解析 event 会直接失败并保持现状。
- `context snapshot`
  - `rd.session.get_context` 返回的 context 级快照。
  - 它汇总当前 runtime 选中的 `session_id`、`capture_file_id`、remote 生命周期、focus 与 recent artifacts，供 Agent 长链调用复用。

## 2. `rd.session.*` 的职责边界

当前公开的 context 工具有两个：

- `rd.session.get_context`
  - 读取当前 context 的只读快照。
  - 返回 `runtime`、`remote`、`focus`、`last_artifacts` 等结构化状态。
- `rd.session.update_context`
  - 只允许补充 user-owned 字段，例如：
    - `focus_pixel`
    - `focus_resource_id`
    - `focus_shader_id`
    - `notes`
  - 不允许手工改写 runtime-owned 字段，例如：
    - `session_id`
    - `capture_file_id`
    - `active_event_id`
    - `remote_id`
    - `last_artifacts`

因此，`rd.session.*` 不是“伪 session 管理器”，而是“context 状态读取与补充入口”。

## 3. `CLI capture open` 实际做了什么

`CLI` 中的：

```bat
rdx capture open --file "C:\path\capture.rdc" --frame-index 0 --connect
```

不是单一 tool，而是对以下平台动作的封装：

1. `rd.core.init`
2. `rd.capture.open_file`
3. `rd.capture.open_replay`
4. `rd.replay.set_frame`
5. 保存本地 session state
6. 如果带 `--connect`，再把状态同步到当前 context 的 daemon

因此，`CLI` 是本地直接执行入口，可供人工、脚本、CI 与本地 Agent 复用；`MCP` 则把同样的底层动作以协议桥接的方式暴露给外部宿主。`CLI` 不是规范源，而是平台动作的 convenience wrapper。

## 4. 状态面与来源优先级

`rdx-tools` 至少存在四类彼此相关但不等价的状态面：

- 本地 session state（local session state）
  - 由 `capture open` 等命令写入本地状态文件，供后续命令读取。
- daemon 状态（daemon state）
  - 记录 daemon 生命周期、context、pipe、已附着 client、部分会话摘要。
- runtime 内部对象
  - 真正的 replay、debug、active event、controller 等进程内对象。
- context 快照（context snapshot）
  - 供 Agent 或自动化读取的 context 级快照，汇总 runtime / remote / focus / recent artifacts。

补充一条 event 语义边界：

- runtime / context 中保存的 `active_event_id` 是 canonical action event。
- `rd.resource.get_usage` / `rd.resource.get_history` 里的 `raw_event_id` 只是底层记录值，不保证能被 `rd.event.*` 直接 round-trip。
- cleanup 顺序按 `rd.capture.close_replay -> rd.capture.close_file` 理解；若 replay 仍活着，`rd.capture.close_file` 会拒绝关闭对应 `capture_file_id`。

理解状态时应按这个顺序思考：

- `capture status` 读的是 local session state，不是直接探测 live runtime。
- `daemon status` 读的是 daemon state，不等价于 local session state，也不保证字段完全同构。
- `rd.session.get_context` 读的是当前 context 的快照，不等于“直接遍历所有 runtime 内部对象”。
- 真正的 live replay/debug 对象存在于 runtime 内部对象层，不能简单由某一份状态文件完全代表。

## 5. `context`、daemon 与 session state

`rdx-tools` 用 `context` 隔离多条工作链路。一个 context 下，常见状态包括：

- daemon 状态（daemon state）
- 本地 session state（local session state）
- runtime 内部对象
- context 快照（context snapshot）

推荐约定：

- 单条调试链路使用 `default`
- 多条独立链路使用自定义 context

这意味着：

- `CLI` 与 `MCP` 可以共用同一套 daemon 机制。
- capture、session、active event、focus、recent artifacts 都按 context 隔离。
- 上层 Agent 如果要跨多轮任务持续工作，优先复用同一 context，而不是把 handle 当作永久主键缓存。

补充一条入口选择原则：

- 能直接访问本地进程、文件系统与 daemon 的宿主，默认 local-first，优先使用 `CLI` 或直接本地 runtime。
- 是否启用 daemon，取决于是否需要长期供应 live runtime / context，而不是取决于是否使用 `MCP`。
- 只有宿主不能直达本地环境，或用户明确要求按 `MCP` 接入时，才应切换到 `MCP`。
- 不论走 `CLI` 还是 `MCP`，上层 Agent 都应先向用户说明当前采用的入口模式。
- 如果选择 `MCP`，但宿主没有配置对应 MCP server，必须显式阻断并提示配置。

## 6. `--connect` 的含义

`CLI` 中不带 `--connect` 时：

- 命令在本次进程内直接执行。
- session state 只保存本地状态文件，便于后续读取。
- 不依赖 daemon 存活。

带 `--connect` 时：

- 命令通过当前 context 的 daemon 执行或复用状态。
- 更适合跨多条命令持续操作同一个 session。
- `MCP` 入口默认也依赖同一套 daemon / context 机制。

## 7. artifact 的角色

artifact 是运行时产物输出目录，默认位于 `intermediate/artifacts/`，可通过 `RDX_ARTIFACT_DIR` 或 `rd.core.init` 中的 `global_env.artifact_dir` 覆盖。

artifact 不是 session 本身，但经常与 session 联动：

- 导出截图
- 导出报告
- 生成 diff 输出
- 写入其他中间产物

因此，上层编排应把 artifact 路径视为“输出位置”，而不是“状态来源”。

`rd.session.get_context` 中的 `last_artifacts` 只是“最近输出索引”，不是 artifact 仓库的规范源。

## 8. 示例与验证口径

文档中的平台链路默认按顺序执行语义描述。

这意味着：

- `capture open -> capture status` 表示顺序调用的推荐链路。
- `rd.remote.connect -> rd.remote.ping -> rd.capture.open_replay` 表示 remote 入口的推荐顺序。
- 除非显式声明支持并发，否则不应把并发观测结果视为平台定义。
- “已验证”必须绑定具体入口和执行方式，例如 `python cli/run_cli.py ...` 的顺序调用，或 `rdx.bat` 交互 shell 中的顺序调用。

## 9. 平台职责与上层职责

`rdx-tools` 仓库负责：

- 暴露稳定的 `rd.*` tool 能力。
- 提供 `.rdc` 到 session 的最小平台链路。
- 说明 `context`、daemon、session、artifact、snapshot 的关系。
- 说明错误恢复的入口与平台约束。

上层 skills / prompts 负责：

- 根据用户目标选择具体 tool 组合。
- 决定后续做 debug、analysis、reverse 或 optimize。
- 在多步任务中组织推理与阶段输出。
- 决定失败后是重试、重建 session、切换 context，还是降级任务目标。

所以，平台文档应说明“系统怎么工作”，而不是“具体任务该怎么做”。
