# tool catalog 入口

`rdx-tools` 当前暴露的规范 `rd.*` tools 以 [spec/tool_catalog.json](../spec/tool_catalog.json) 为准。

当前公开 catalog 的能力边界是“打开 `.rdc` 后做离线 replay / 调试 / 导出”；不提供 `rd.app.*` 一类 app-side integration 控制面，也不暗示可控制任意 app。

本文只承担 catalog 入口职责，不承担使用教程。如何建立 session、如何理解 `context` 与 daemon、如何给 Agent 写平台说明，请分别参考：

- [session-model.md](session-model.md)
- [agent-model.md](agent-model.md)
- [quickstart.md](quickstart.md)

## 规范源说明

理解本仓库时，请记住：

- `spec/tool_catalog.json` 与共享响应契约是 tool 能力面与参数语义的规范源。
- runtime 行为是平台真相的运行时体现。
- `CLI` 只是部分平台动作的 convenience wrapper，不是完整能力面的等价镜像，也不是规范源。
- 规范定义以 `spec/tool_catalog.json` 为准。
- catalog 现在包含结构化 `prerequisites`；它是 Agent 做静态前置推理的第一入口，不应再把 tool 顺序知识隐藏在文档段落或运行时报错里。

当前分类边界补充如下：

- `rd.export.*` 是唯一的文件导出分类面；纹理、buffer、mesh 的落盘导出统一从这里进入。
- `rd.texture.*` / `rd.buffer.*` / `rd.mesh.*` 负责资源读取、检查、结构化解析与预览，不再额外暴露平行的导出 public surface。
- `rd.macro.*` 只保留多步工作流与高阶报告入口，不再保留一跳 passthrough 式 macro。
- `rd.analysis.*` 已收敛移除；分析入口通过 retained `rd.macro.*`、`rd.diag.*` 与少量 canonical inspection tools 组合完成。

补充一条入口边界：

- `CLI` 是 daemon-backed 本地命令入口。
- daemon 是长生命周期 runtime / context 持有层。
- `MCP` 是把 catalog 能力桥接给外部宿主的协议入口。
- catalog 本身不偏向 `CLI` 或 `MCP`；两者都依赖同一 daemon-owned runtime / context。

## 能力定位优先级

默认推荐顺序固定为：

1. canonical `rd.*` 是主接口
2. `rd.macro.*` 是高阶工作流
3. `rd.session.*` / `rd.core.*` 负责 context、恢复与 discovery
4. `rd.vfs.*` 是导航辅助层
5. `tabular/tsv projection` 是展示投影，不是独立能力面

补充说明：

- 主调试接口始终是 canonical `rd.*`。
- `rd.vfs.*` 适合浏览结构、探索路径、快速查看当前 session 的层级视图。
- `tabular/tsv projection` 是对结构化结果的表格化摘要，目的是提升扫描效率，不表示经过语义重要度排序。

## 当前公开辅助入口

当前公开 catalog 已包含：

- `rd.vfs.ls`
- `rd.vfs.cat`
- `rd.vfs.tree`
- `rd.vfs.resolve`
- `rd.session.get_context`
- `rd.session.update_context`
- `rd.session.list_sessions`
- `rd.session.select_session`
- `rd.session.resume`
- `rd.core.get_operation_history`
- `rd.core.get_runtime_metrics`
- `rd.core.list_tools`
- `rd.core.search_tools`
- `rd.core.get_tool_graph`

其中 `rd.vfs.*` 的定位是：

- 只读探索层，主要服务人类与 Agent 的路径式浏览。
- canonical truth 仍然是结构化 JSON；其中 `rd.vfs.ls` 可额外请求统一 tabular projection 作为 entries 摘要，但这只是展示投影，不构成独立能力面。
- `rd.vfs.*` 只负责导航、解析和读取，不负责修改 runtime、切换 event、导出资源或更新 context。
- 真正的 canonical tools 仍然是原有 `rd.*` 结构化接口；`rd.vfs.*` 会把这些 canonical tools 作为节点元数据暴露出来。
- 需要精确读取字段、切换 event、导出证据、写自动化链路时，应回到 canonical `rd.*`。

其中 `rd.session.*` 用于暴露 context snapshot：

- 读取当前 context 的 runtime / remote / focus / recent artifacts 状态。
- 读取并切换 `current_session_id` 与 `sessions` 表。
- 暴露 `recovery`、`limits`、`active_operation` 与 `recent_operations`。
- 让上层 Agent 只补充 user-owned 字段，例如 `focus_pixel`、`focus_resource_id`、`focus_shader_id`、`notes`。
- 不允许人工或 Agent 通过它们直接篡改 runtime-owned 字段，如 `session_id`、`capture_file_id`、`active_event_id`、`remote_id`。

其中新增 `rd.core.*` discovery / observability 入口用于：

- 读取 trace-linked 操作历史与当前 runtime 自监控指标。
- 按 `namespace`、`group`、`capability`、`role`、`intent` 做轻量 tool discovery，并默认优先推荐 canonical `rd.*`，再是 macro、context/core 元信息层与 navigation 层。
- 显式返回 prerequisite 与 macro-to-canonical 依赖图，而不是要求 Agent 自己从完整 catalog 描述中猜调用链。

## Event 语义

- `rd.event.set_active` 只接受可被 action tree 解析的 canonical `event_id`；失败不会污染 runtime / context 中现有的 `active_event_id`。
- `rd.event.get_actions` 与 `rd.event.get_action_tree` 现在默认走有界返回，并通过 `pagination` 暴露是否截断；大 capture 下不再默认一次性物化整棵事件树。
- `rd.pipeline.*` 的同次调用内，snapshot 与 live pipeline 读取共享同一个已解析 event 上下文，不允许前后错位。
- event-bound `rd.pipeline.*`、`rd.shader.*`、`rd.texture.get_pixel_value`、`rd.export.shader_bundle` 与 `rd.shader.debug_start` 会返回 `resolved_event_id`；如果 backend 不能精确绑定请求 event，必须显式失败，不允许 silent fallback。
- `rd.resource.get_usage` / `rd.resource.get_history` 会同时暴露：
  - canonical `event_id`
  - `raw_event_id`
  - `event_resolvable`
- 只有 canonical `event_id` 可以直接作为 `rd.event.*` 输入；`raw_event_id` 仅用于诊断底层 RenderDoc 记录。

## Shader 替换与调试口径

- `rd.shader.edit_and_replace` 现在要么执行真实 runtime shader replacement，要么返回明确的 capability/runtime 失败；不再允许 `mock_applied` 一类伪成功。
- `rd.shader.edit_and_replace` 在编译阶段会传入真实 `ShaderCompileFlags` 对象；若编译失败、`BuildTargetShader` 绑定失败或 `ReplaceResource` 失败，错误会通过结构化 `error.code/details` 暴露，而不是全部折叠成同一种失败。
- `rd.shader.edit_and_replace` 现在支持 `emit_patch_artifacts` 与 `output_dir`，可直接导出改前 IR、改后 IR 与 unified diff，便于对照手工 `qrenderdoc` patch 流程。
- `force_full_precision` 在 `SPIR-V (RenderDoc)` 目标下会把“本次到底命中了哪些 `RelaxedPrecision` 行”写进 `messages`；若变量没有直接命中任何 `RelaxedPrecision` 行，则会返回 `status="noop"` 并明确说明“matched no RelaxedPrecision lines for variables: ...”。
- 对 Android remote Vulkan 的手动 IR 调试，不要只看一个采样点；像 `EventID 1248` 这类 shader，`variables=["404"]` 这类高影响补丁可能只命中一行 `RelaxedPrecision`，但会把整个 `ResourceId::208592` 输出面一起打成 `0`。应同时用 `rd.texture.get_pixel_value`、`rd.export.screenshot` 与 `rd.shader.revert_replacement` 交叉验证。
- 当 `rd.shader.edit_and_replace` 返回 `status="noop"` 时，表示当前 session 中没有创建 live replacement；这时不应再把该 `replacement_id` 当成需要回滚的 active replacement。
- `rd.shader.debug_start` 只在请求 event 的真实 debug 上下文可用时成功；如果只能跨 event 或 synthetic 回退，运行时会显式失败。
- `rd.shader.debug_start` 失败时会保留 `failure_stage` / `failure_reason`、`attempts`、`pixel_history_summary` 与 `resolved_context`，用于区分 target 配置失败、cross-event only、invalid trace 或 debugger handle 缺失。
- `rd.export.shader_bundle` 会按请求 `event_id` 导出，并把 `requested_event_id` / `resolved_event_id` 一起写入 bundle。

## Remote 说明

- `rd.remote.connect` 返回的 `remote_id` 代表 live remote connection；若连接失败，不会返回占位 handle。
- `rd.capture.open_replay` 的 remote 入口是 `options.remote_id`，而不是隐式回退到 `localhost`。
- remote replay 成功后，原 `remote_id` 默认仍保持 live；其 replay-owned lease 会反映在 `rd.session.get_context -> remote.active_session_ids`。
- 当 live remote handle 仍被 lease 时，`rd.remote.disconnect` 预期返回 `remote_handle_in_use`；不要把这种情况误判成 endpoint 丢失。
- `remote_handle_consumed` 只应出现在旧 tombstone / 显式 consumed state 恢复路径，不再是正常成功链路的默认语义。
- daemon / worker 重启后，平台会优先基于持久化 remote 元数据恢复同一个 `session_id`；只有 endpoint 真断开、bootstrap 失败或恢复元数据缺失时，才会显式进入 `degraded` / error。
- `rd.remote.connect` 与 `rd.capture.open_replay` 会更新结构化 progress；daemon 路径下应通过 `daemon status/get_state -> active_operation` 读取统一状态面。
- `rd.remote.connect` 的 `options` 参数面在 `CLI` / daemon / `MCP` 下保持一致。
- `rd.shader.compile` 现在接受可选 `session_id` 与 `source_encoding`；不同 replay backend 的 `supported_source_encodings` 可能不同。
- `rd.remote.set_overlay_options` 在当前 `RenderDoc` Python binding 未暴露 overlay RPC 时，会显式返回 `remote_overlay_options_unavailable`。

## 权威来源

- `spec/tool_catalog.json`
- 共享响应契约

## 校验

```bat
python spec/validate_catalog.py
```
