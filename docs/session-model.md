# Session 模型

本文说明 `rdx-tools` 的平台使用模型：怎样把一份 `.rdc` 变成可操作的 session，以及 `context`、daemon、session state、artifact 分别承担什么职责。

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
  - 它同样是运行时句柄，不应被视为长期稳定标识。
- `frame_index`
  - 当前 replay 所选帧。
- `active_event_id`
  - 当前焦点事件，常作为后续 event 级分析的起点。

## 2. `CLI capture open` 实际做了什么

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

因此，`CLI` 适合人工快速上手；`MCP` 则把同样的底层动作显式暴露给 client / Agent 自行编排。`CLI` 不是规范源，而是平台动作的 convenience wrapper。

## 3. 状态面与来源优先级

`rdx-tools` 至少存在三类彼此相关但不等价的状态面：

- local session state
  - 由 `capture open` 等命令写入本地状态文件，供后续命令读取。
- daemon state
  - 记录 daemon 生命周期、context、pipe、已附着 client、部分会话摘要。
- runtime 内部对象
  - 真正的 replay、debug、active event、controller 等进程内对象。

理解状态时应按这个顺序思考：

- `capture status` 读的是 local session state，不是直接探测 live runtime。
- `daemon status` 读的是 daemon state，不等价于 local session state，也不保证字段完全同构。
- 真正的 live replay/debug 对象存在于 runtime 内部对象层，不能简单由某一份状态文件完全代表。

## 4. `context`、daemon 与 session state

`rdx-tools` 用 `context` 隔离多条工作链路。一个 context 下，常见状态包括：

- daemon state
- local session state
- runtime 内部对象

推荐约定：

- 单条调试链路使用 `default`
- 多条独立链路使用自定义 context

这意味着：

- `CLI` 与 `MCP` 可以共用同一套 daemon 机制。
- 但 capture、session、active event、debug 状态按 context 隔离。

## 5. `--connect` 的含义

`CLI` 中不带 `--connect` 时：

- 命令在本次进程内直接执行。
- session state 只保存本地状态文件，便于后续读取。
- 不依赖 daemon 存活。

带 `--connect` 时：

- 命令通过当前 context 的 daemon 执行或复用状态。
- 更适合跨多条命令持续操作同一个 session。
- `MCP` 入口默认也依赖同一套 daemon / context 机制。

## 6. artifact 的角色

artifact 是运行时产物输出目录，默认位于 `intermediate/artifacts/`，可通过 `RDX_ARTIFACT_DIR` 或 `rd.core.init` 中的 `global_env.artifact_dir` 覆盖。

artifact 不是 session 本身，但经常与 session 联动：

- 导出截图
- 导出报告
- 生成 diff 输出
- 写入其他中间产物

因此，上层编排应把 artifact 路径视为“输出位置”，而不是“状态来源”。

## 7. 示例与验证口径

文档中的平台链路默认按顺序执行语义描述。

这意味着：

- `capture open -> capture status` 表示顺序调用的推荐链路。
- 除非显式声明支持并发，否则不应把并发观测结果视为平台定义。
- “已验证”必须绑定具体入口和执行方式，例如 `python cli/run_cli.py ...` 的顺序调用，或 `rdx.bat` 交互 shell 中的顺序调用。

## 8. 平台职责与上层职责

`rdx-tools` 仓库负责：

- 暴露稳定的 `rd.*` tool 能力。
- 提供 `.rdc` 到 session 的最小平台链路。
- 说明 `context`、daemon、session、artifact 的关系。
- 说明错误恢复的入口与平台约束。

上层 skills / prompts 负责：

- 根据用户目标选择具体 tool 组合。
- 决定后续做 debug、analysis、reverse 或 optimize。
- 在多步任务中组织推理与阶段输出。
- 决定失败后是重试、重建 session、切换 context，还是降级任务目标。

所以，平台文档应说明“系统怎么工作”，而不是“具体任务该怎么做”。
