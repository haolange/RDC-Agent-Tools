# tool catalog 入口

`rdx-tools` 当前暴露 catalog 定义的 `202` 个规范 `rd.*` tools。

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

补充一条入口边界：

- `CLI` 是本地直接执行入口。
- daemon 是长生命周期 runtime / context 持有层。
- `MCP` 是把 catalog 能力桥接给外部宿主的协议入口。
- catalog 本身不偏向 `CLI` 或 `MCP`；入口选择取决于宿主是否能直接进入本地环境，以及任务是否需要长期供应 live runtime / context。

## 当前新增入口

当前公开 catalog 已包含：

- `rd.vfs.ls`
- `rd.vfs.cat`
- `rd.vfs.tree`
- `rd.vfs.resolve`
- `rd.session.get_context`
- `rd.session.update_context`

其中 `rd.vfs.*` 的定位是：

- 只读探索层，主要服务人类与 Agent 的路径式浏览。
- 输出仍然是结构化 JSON，不再引入第二套 TSV/文本真相。
- `rd.vfs.*` 只负责导航、解析和读取，不负责修改 runtime、切换 event、导出资源或更新 context。
- 真正的 canonical tools 仍然是原有 `rd.*` 结构化接口；`rd.vfs.*` 会把这些 canonical tools 作为节点元数据暴露出来。

其中 `rd.session.*` 用于暴露 context snapshot：

- 读取当前 context 的 runtime / remote / focus / recent artifacts 状态。
- 让上层 Agent 只补充 user-owned 字段，例如 `focus_pixel`、`focus_resource_id`、`focus_shader_id`、`notes`。
- 不允许人工或 Agent 通过它们直接篡改 runtime-owned 字段，如 `session_id`、`capture_file_id`、`active_event_id`、`remote_id`。

## Event 语义

- `rd.event.set_active` 只接受可被 action tree 解析的 canonical `event_id`；失败不会污染 runtime / context 中现有的 `active_event_id`。
- `rd.pipeline.*` 的同次调用内，snapshot 与 live pipeline 读取共享同一个已解析 event 上下文，不允许前后错位。
- `rd.resource.get_usage` / `rd.resource.get_history` 会同时暴露：
  - canonical `event_id`
  - `raw_event_id`
  - `event_resolvable`
- 只有 canonical `event_id` 可以直接作为 `rd.event.*` 输入；`raw_event_id` 仅用于诊断底层 RenderDoc 记录。

## Remote 说明

- `rd.remote.connect` 返回的 `remote_id` 代表 live remote connection；若连接失败，不会返回占位 handle。
- `rd.capture.open_replay` 的 remote 入口是 `options.remote_id`，而不是隐式回退到 `localhost`。
- remote replay 成功后，原 `remote_id` 会进入 consumed 生命周期语义，不再能继续 `ping` / `disconnect` / `open_replay`。
- 若后续继续对旧 `remote_id` 调用 remote tool，预期会得到 `remote_handle_consumed` 一类生命周期错误，而不是“平台随机坏了”。
- `rd.remote.connect` 与 `rd.capture.open_replay` 会更新结构化 progress；daemon 路径下应通过 `daemon status/get_state -> active_operation` 读取统一状态面。

## 权威来源

- `spec/tool_catalog.json`
- 共享响应契约

## 校验

```bat
python spec/validate_catalog.py
```
