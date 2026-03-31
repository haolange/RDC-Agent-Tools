# Session 模型

本文说明 `rdx-tools` 的平台使用模型：怎样把一份 `.rdc` 变成可操作的 session，以及 `context`、daemon、artifact、context snapshot 分别承担什么职责。

本文讨论的公开能力边界仅限于 `.rdc` 离线 replay / 调试 / 导出，不包含应用侧集成或任意 app 控制语义。

本文不讨论上层业务 workflow。shader debug、reverse、analysis、optimize 等任务策略应由上层 skills、system prompt、reference docs 决定。

## 1. 最小对象链路

一份 `.rdc` 进入 `rdx-tools` 后，典型平台链路是：

```text
.rdc
-> capture_file_id
-> session_id
-> frame_index / active_event_id
-> preview (optional human observer)
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
  - remote `open_replay` 成功后，会基于该 live handle 建立 replay-owned lease；默认不会把原 `remote_id` 从 live endpoint 池中移除。
  - `rd.session.get_context` 的 `remote.active_session_ids` 会显式反映这个 live handle 当前被哪些 replay session lease。
  - 当 `active_session_ids` 非空时，`rd.remote.disconnect` 预期返回 `remote_handle_in_use`；应先关闭 replay 或等待 lease 释放。
  - `remote_handle_consumed` 仍可能出现在旧状态恢复或显式 tombstone 场景里，但它不再是正常 remote `open_replay` 成功后的默认语义。
  - 当 remote replay session 建成后，平台会把 `transport`、endpoint、`origin_remote_id`、Android `device_serial`、bootstrap 摘要等恢复元数据写入持久化 session record，用于后续恢复同一个 `session_id`。
  - 它同样是运行时句柄，不应被视为长期稳定标识。
- `frame_index`
  - 当前 replay 所选帧。
- `active_event_id`
  - 当前焦点 action event，常作为后续 event 级分析的起点。
  - 只有可被 `rd.event.get_action_details` round-trip 的 event 才会被写入这里；`rd.event.set_active` 对不可解析 event 会直接失败并保持现状。
- `context snapshot`
  - `rd.session.get_context` 返回的 context 级快照。
  - 它汇总当前 runtime 选中的 `session_id`、`capture_file_id`、remote 生命周期、focus 与 recent artifacts，供 Agent 长链调用复用。
- `preview`
  - 绑定当前 context 的人类同步观察窗口。
  - 固定跟随 `current_session_id + active_event_id`。
  - 默认显示当前 event 的完整 framebuffer / RT，不按 viewport 裁小；若当前 event 存在 viewport / scissor，会以区域标识叠加到完整 framebuffer 上。
  - `rd.session.get_context.preview.display` 会暴露 `output_slot`、`texture_id`、`texture_format`、`framebuffer_extent`、`viewport_rect`、`scissor_rect`、`effective_region_rect`、`window_rect`、`fit_mode` 与 `screen_cap_ratio`。
  - 它是 human observer，不是新的 runtime truth，也不是 fix verification / evidence 输入。
- `persistent context state`
  - daemon-backed 持久化索引，保存当前 context 的 `captures`、`sessions`、`current_session_id`、`recovery`、`limits` 与 `recent_operations`。
  - daemon 重启后，本地与可恢复 remote session 的恢复都以它为准；`context snapshot` 只是当前视角投影。

## 2. `rd.session.*` 的职责边界

当前公开的 context 工具有五个：

- `rd.session.get_context`
  - 读取当前 context 的只读快照。
  - 返回 `runtime`、`remote`、`focus`、`last_artifacts`、`preview`、`runtime_parallelism_ceiling` 等结构化状态。
  - 其中 `preview.display` 是人类观察面的几何元数据，不是新的 gate / evidence 真相。
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
- `rd.session.open_preview`
  - 为当前 context 的 current session 打开或重绑定 preview。
  - 如果给了 `session_id`，它必须和当前 context 的 current session 一致；否则应先 `rd.session.select_session`。
- `rd.session.close_preview`
  - 关闭当前 context 的 preview，并清除该 context 的 preview enabled intent。
- `rd.session.list_sessions`
  - 返回当前 context 的 session 表、`current_session_id` 与 `runtime_parallelism_ceiling`。
- `rd.session.select_session`
  - 只切换当前 context 的 current session 指针，不销毁其他 session 记录。
- `rd.session.resume`
  - 基于持久化索引恢复当前 context 的本地与可恢复 remote session，并优先复用原 `session_id`。
  - 若 remote endpoint 真断开、Android bootstrap 失败或恢复元数据缺失，会显式返回 `degraded` / error，而不是把上层链路静默切到别的 session。

新增的 context / owner / baton 工具有八个：

- `rd.session.create_context`
- `rd.session.list_contexts`
- `rd.session.select_context`
- `rd.session.clear_context`
- `rd.session.claim_runtime_owner`
- `rd.session.release_runtime_owner`
- `rd.session.export_runtime_baton`
- `rd.session.rehydrate_runtime_baton`

因此，`rd.session.*` 不是“伪 session 管理器”，而是“context 状态读取、session 选择与恢复入口”。

补充边界：

- `runtime_parallelism_ceiling` 只描述 transport/runtime 层的能力上限。
- local ceiling 为 `multi_context_multi_owner`，remote ceiling 固定为 `single_runtime_owner`。
- 上层 Framework 可把 local ceiling 消费成 `concurrent_team` 或 `staged_handoff` 的 orchestrated multi-context，但 Tools 不负责替宿主选择 coordination policy。
- `session_locator` 只对齐 `rdc_path` / `session_id` / `frame_index` / `active_event_id`，用于关联与恢复提示；它不是稳定的跨进程 handle。
- 这不等于所有宿主都能并发多 live owners；平台是否允许 team-style coordination 由上层 Frameworks 决定。

## 3. `CLI capture open` 实际做了什么

`CLI` 中的：

```bat
rdx capture open --file "C:\path\capture.rdc" --frame-index 0
```

不是单一 tool，而是对以下平台动作的封装：

1. `rd.core.init`
2. `rd.capture.open_file`
3. `rd.capture.open_replay`
4. `rd.replay.set_frame`
5. 由当前 context 的 daemon 统一持有 runtime / context 状态

因此，`CLI` 是 daemon-backed 本地命令入口，可供人工、脚本、CI 与本地 Agent 复用；`MCP` 则把同样的底层动作以协议桥接的方式暴露给外部宿主。两者都不拥有独立 runtime；`CLI` 不是规范源，而是平台动作的 convenience wrapper。

额外约束：

- `capture open` 只建立 tools-layer session state，不创建任何 framework `workspace/case/run`。
- `workspace/case/run` 是否创建，属于上层 framework intake / orchestration 合同，不属于 `rdx-tools` 平台契约。

## 4. 状态面与来源优先级

`rdx-tools` 至少存在五类彼此相关但不等价的状态面：

- daemon 状态（daemon state）
  - 记录 daemon 生命周期、context、pipe、已附着 client、部分会话摘要。
- runtime 内部对象
  - 真正的 replay、debug、active event、controller 等进程内对象。
- context 快照（context snapshot）
  - 供 Agent 或自动化读取的 context 级快照，汇总 runtime / remote / focus / recent artifacts。
- preview 观察面（preview observer）
  - 只给人类同步看当前 active event 的可视结果。
  - 它显示的是完整 framebuffer / RT，并把 viewport / scissor 作为区域标识叠加进去，而不是默认裁小显示区域。
  - 公开状态固定通过 `rd.session.get_context.preview` 暴露。
  - 它不会升级成 runtime truth，也不会替代 canonical `rd.*` 证据。
- persistent context state
  - 记录 `captures`、`sessions`、`current_session_id`、`recovery`、`limits` 与 `recent_operations`，是本地恢复与多 session 选择的持久化真相。

补充一条 event 语义边界：

- runtime / context 中保存的 `active_event_id` 是 canonical action event。
- `rd.resource.get_usage` / `rd.resource.get_history` 里的 `raw_event_id` 只是底层记录值，不保证能被 `rd.event.*` 直接 round-trip。
- event-bound `rd.pipeline.*`、`rd.shader.*`、`rd.texture.get_pixel_value`、`rd.export.shader_bundle` 与 `rd.shader.debug_start` 现在都应把最终使用的 event 通过 `resolved_event_id` 回传给上层；如果 backend 不能精确绑定请求 event，应显式返回 capability/runtime 失败。
- `rd.shader.edit_and_replace` 的成功不再是“逻辑记录”，而是绑定到真实 runtime replacement；若替换链路在 `BuildTargetShader`、编译诊断或 `ReplaceResource` 任一步失败，session/context 应保留结构化失败细节，而不是写入伪成功 replacement。
- replacement 成功后，后续 `rd.export.screenshot`、`rd.texture.get_data`、`rd.texture.get_pixel_value` 都必须重新回到同一个 live replay event 读取；它们与 replacement 共享 `resolved_event_id` 与同一套 visual target 解析链。
- `rd.shader.debug_start` 若失败，也应把 `failure_stage` / `failure_reason`、`attempts`、`pixel_history_summary` 与 `resolved_context` 作为本次 event-bound debug 的真实诊断面保留下来，供同一 `session_id` 后续排查复用。
- cleanup 顺序按 `rd.capture.close_replay -> rd.capture.close_file` 理解；若 replay 仍活着，`rd.capture.close_file` 会拒绝关闭对应 `capture_file_id`。

理解状态时应按这个顺序思考：

- `capture status` 读的是当前 context 的 daemon / snapshot 摘要，不是 adapter-local 状态文件。
- `daemon status` 读的是 daemon state，不等于“直接遍历所有 runtime 内部对象”。
- `rd.session.get_context` 读的是当前 context 的快照与持久化索引组合视图，不等于“直接遍历所有 runtime 内部对象”。
- `rd.session.list_sessions` / `rd.session.resume` 面向持久化状态索引，不等于“直接遍历所有 runtime 内部对象”。
- `rd.session.get_context.preview` 读的是当前 context 的 preview observer 状态，不等于“当前窗口一定存在且可替代结构化证据”。
- 真正的 live replay/debug 对象存在于 runtime 内部对象层，不能简单由某一份状态文件完全代表。
- `last_artifacts` 是有界 recent index，而不是 artifact 仓库本身；当前 retention policy 默认为 `total_limit=32`、`per_type_limit=8`。
- remote 相关的真实能力面不会再被压扁进 `runtime_parallelism_ceiling`；需要区分 remote endpoint / replay / event-bound inspection / shader debug / shader replace / shader compile / fix verification 时，应读取 `rd.session.get_context -> remote_capability_matrix`。
- `remote_context_locality=strict` 与 `remote_handle_reuse_policy=must_reconnect` 是 context-scope hard contract；remote handle 不能跨 context 复用，也不能在需要 remote replay 时静默掉回 local。

## 5. `context`、daemon 与 session state

`rdx-tools` 用 `context` 隔离多条工作链路。一个 context 下，常见状态包括：

- daemon 状态（daemon state）
- runtime 内部对象
- context 快照（context snapshot）
- persistent context state

推荐约定：

- 单条调试链路使用 `default`
- 多条独立链路使用自定义 context

这意味着：

- `CLI` 与 `MCP` 可以共用同一套 daemon 机制。
- capture、session、active event、focus、recent artifacts、recent operations 都按 context 隔离。
- 一个 context 现在可以同时持有多条本地 session 记录；`current_session_id` 只表示当前选中的工作面，而不是该 context 唯一能存在的 session。
- 上层 Agent 如果要跨多轮任务持续工作，优先复用同一 context，而不是把 handle 当作永久主键缓存。
- `rdx daemon stop` 只停止 daemon，不会清空该 context 的本地恢复索引；真正销毁状态要执行 `rdx context clear` 或 `rd.core.shutdown`。
- preview 是 context 级 enabled intent：`rdx daemon stop` / worker 重启会关闭 live 窗口，但保留 enabled intent；后续同一 context 恢复 live session 时会自动重绑。
- 但如果第一次 `rd.session.open_preview` 就失败，context 会保留 `enabled=false`、清空 `bound_*` 字段并写入 `last_error`；只有真实建窗成功后才会写入 enabled intent。
- `rd.session.close_preview`、`rdx context clear` 与 `rd.core.shutdown` 会关闭窗口并清掉该 intent。
- 当 preview 已 enabled 时，`rd.event.set_active`、`rd.replay.set_frame`、`rd.session.select_session` 与 `rd.session.resume` 会在返回前先完成一次 preview 同步刷新尝试；刷新失败只更新 preview 状态，不回滚 runtime 主真相。
- preview 窗口会按 framebuffer 几何自动调整大小，并把默认上限限制在当前屏幕工作区的 `50%`；若用户手动拖拽过窗口，在 framebuffer 几何不变时不会被持续覆盖。
- remote 一律采用 `single_runtime_owner`；高能力平台的差异只体现在 local 是否允许 multi-context 并行，而不是允许多个 owner 共享同一条 remote live runtime。

补充一条入口选择原则：

- 能直接访问本地进程、文件系统与 daemon 的宿主，默认 local-first，优先使用 daemon-backed `CLI`。
- 只有宿主不能直达本地环境，或用户明确要求按 `MCP` 接入时，才应切换到 `MCP`。
- 不论走 `CLI` 还是 `MCP`，上层 Agent 都应先向用户说明当前采用的入口模式。
- 如果选择 `MCP`，但宿主没有配置对应 MCP server，必须显式阻断并提示配置。

## 6. daemon-backed `CLI`

`CLI` 中所有业务命令都通过当前 context 的 daemon 执行或复用状态。

这意味着：

- `CLI` 与 `MCP` 默认依赖同一套 daemon / context 机制。
- 长操作期间的中间状态以 daemon `active_operation` 为准，而不是靠日志文本猜测。
- `CLI` 不再提供独立 runtime / session truth。

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
- “已验证”必须绑定具体入口和执行方式，例如 `rdx.bat --non-interactive cli ...` 的顺序调用，或 `rdx.bat` 交互 shell 中的顺序调用。
- `rdx.bat --non-interactive` 在子命令返回 canonical JSON 时会直接透传完整 payload；自动化脚本应按完整 payload 读取，而不是依赖旧的短状态壳。

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
