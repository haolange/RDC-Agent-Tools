# Tool Catalog

`rdx-tools` 当前暴露 catalog 定义的 `198` 个规范 `rd.*` tools。

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

## 当前新增入口

当前公开 catalog 已包含：

- `rd.session.get_context`
- `rd.session.update_context`

它们用于暴露 context snapshot：

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

## 权威来源

- `spec/tool_catalog.json`
- 共享响应契约

## 校验

```bat
python spec/validate_catalog.py
```
