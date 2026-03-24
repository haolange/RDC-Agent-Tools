# 快速开始

本文只覆盖“最短上手路径”，帮助你确认 `rdx-tools` 的入口可用，并把一份 `.rdc` 变成可操作的 session。更完整的状态模型见 [session-model.md](session-model.md)。

仓库默认公开的能力面聚焦于 `.rdc` 离线 replay / 调试 / 导出，不包含 app-side integration 控制链路。

## 1. 先验证入口

在仓库根目录执行：

```bat
python cli/run_cli.py --help
python mcp/run_mcp.py --help
python spec/validate_catalog.py
rdx.bat --non-interactive cli --help
```

如果以上命令都可运行，再继续下面的 `CLI` 或 `MCP` 路径。

## 2. local-first 使用 `CLI`

可直接访问本地环境时，最方便的入口是：

```bat
rdx.bat
```

主菜单为：

```text
1. Start CLI
2. Start MCP
3. Help
0. Exit
```

选择 `1. Start CLI` 后，launcher 会让你选择 `default` 或自定义 context，然后打开一个持续可用的 `CLI` shell。

### 最小链路

在 `CLI` shell 中执行：

```bat
rdx capture open --file "C:\path\capture.rdc" --frame-index 0
rdx capture status
rdx call rd.event.get_actions --args-file ".\args.json" --format json
rdx daemon status
```

其中 `args.json` 应是 UTF-8 JSON object，例如：

```json
{"session_id":"<session_id>"}
```

你会得到至少这些关键信息：

- `capture_file_id`
- `session_id`
- `active_event_id`

其中 `active_event_id` 只会写成可被 `rd.event.get_action_details` round-trip 的 action event。若 `rd.event.set_active` 收到不可解析的 `event_id`，调用会失败，且不会污染当前 context。
`rdx capture open` 只负责建立当前 context 的 capture/session state，不会创建上层 framework 的 `workspace/case/run`。

若后续要做清理，推荐顺序是先 `rd.capture.close_replay`，再 `rd.capture.close_file`。当 capture 仍被 live replay 持有时，`rd.capture.close_file` 会返回失败而不是静默移除 handle。

如果后续要把同一条链路交给上层 Agent 继续使用，建议额外查看：

```bat
rdx call rd.session.get_context --format json
rdx call rd.session.list_sessions --format json
```

如果 daemon 因显式 `stop`、shell 退出或异常退出而中断，再次附着同一 context 时，可直接读取：

```bat
rdx call rd.session.get_context --format json
rdx call rd.session.resume --format json
```

当前平台会优先按持久化索引恢复本地与可恢复 remote session，并尽量复用原 `session_id`。如果 remote endpoint 真断开、bootstrap 失败或恢复元数据缺失，恢复会显式进入 `degraded` / error，而不是把上层调用静默打成新的未知 session。

### 可选：用 `VFS` 快速浏览当前 session

如果想用只读路径式方式快速探索当前 frame，也可以执行：

```bat
rdx vfs ls --path / --format json
rdx vfs ls --path / --format tsv
rdx vfs tree --path /draws --depth 2 --format json
rdx vfs cat --path /pipeline --format json
```

`rd.vfs.*` / `rdx vfs *` 只负责导航与读取；真正的修改、导出、切换与 context 更新仍继续走原有 `rd.*` tools。
其中 `--format tsv` 只是对结构化结果的表格化摘要，用于更快扫描列表，不表示语义重要度排序。

### 结束与清理

```bat
rdx daemon stop
rdx context clear
```

`exit` / `quit` 只退出当前 shell，不会自动停止 daemon，也不会自动清理 context。
`rdx daemon stop` 只停止 daemon，默认保留本地 `.rdc` 的持久化恢复状态。
`rdx context clear` 才会显式销毁当前 context 的持久化 session/capture 索引与 snapshot。

## 3. 对接 `MCP` client

`MCP` 入口适合无法直接进入本地环境的外部 `MCP` client / Agent，或用户明确要求按 `MCP` 接入的场景。你可以通过 launcher 启动，也可以直接用脚本启动。

在进入任一路径前，建议先明确两件事：

- 当前任务采用 `CLI` 还是 `MCP`
- 如果采用 `MCP`，宿主是否已经配置对应 MCP server；未配置时必须先阻断并提示配置

### 先做环境检查

```bat
python mcp/run_mcp.py --ensure-env --daemon-context smoke-test
```

### 通过 launcher 启动

```bat
rdx.bat
```

选择 `2. Start MCP`，再选择 transport：

- `stdio`
  - 没有 URL。
  - 适合由外部 client 接管标准输入输出。
- `streamable-http`
  - 会显示 `http://<host>:<port>`。
  - 适合通过 HTTP 访问。

### 直接启动脚本

```bat
python mcp/run_mcp.py --transport streamable-http --host 127.0.0.1 --port 8765 --daemon-context smoke-test
```

如需通过 launcher 直接走当前正式非交互 `CLI` passthrough，也可以执行：

```bat
rdx.bat --non-interactive cli --daemon-context smoke daemon status
```

当子命令返回 canonical JSON 时，`rdx.bat --non-interactive` 会直接输出完整 payload，适合脚本与自动化读取。

## 4. `MCP` 最小工具链路

对接 `MCP` client 后，建议先完成这条平台级最小链路：

1. `rd.core.init`
2. `rd.capture.open_file`
3. `rd.capture.open_replay`
4. `rd.replay.set_frame`
5. `rd.event.get_actions`
6. 需要确认当前 context 状态时，调用 `rd.session.get_context`
7. 一个 context 同时持有多条 session 时，使用 `rd.session.list_sessions` / `rd.session.select_session`

这条链路只负责建立可操作 session，不代表任何上层 debug 或 analysis workflow。

### 4.1 Android remote 最小链路

如果目标是 Android remote replay / debug，建议按这条顺序链路执行：

1. `rd.core.init`
2. `rd.remote.connect`，并在 `options` 中传 `transport="adb_android"`
3. `rd.remote.ping`
4. `rd.capture.open_file`
5. `rd.capture.open_replay`，并在 `options.remote_id` 中传上一步返回的 `remote_id`
6. `rd.replay.set_frame`
7. 如需给上层 Agent 记录焦点状态，可调用 `rd.session.update_context`

关键约束：

- `rd.remote.connect` 会负责 Android `adb` bootstrap：选择设备、选择仓库内 APK、启动 `RenderDocCmd`、push `renderdoc.conf`、建立 `adb forward`。
- 如果 `rd.remote.connect` 失败，不应继续盲跑依赖 `remote_id` 的后续链路。
- 如果 `rd.capture.open_replay(options.remote_id=...)` 成功，原 `remote_id` 会被消费；如需新的 live handle，必须重新 `rd.remote.connect`。
- daemon / worker 重启后，平台会优先使用持久化 remote 元数据恢复同一个 `session_id`；只有 endpoint 真断开、bootstrap 失败或恢复元数据不足时，才需要重新执行 `rd.remote.connect -> rd.remote.ping -> rd.capture.open_replay`。
- 对 event-bound 链路，优先显式传入 `event_id`，并检查返回里的 `resolved_event_id`；`rd.shader.debug_start`、`rd.export.shader_bundle`、`rd.pipeline.get_shader`、`rd.shader.get_reflection`、`rd.shader.get_disassembly`、`rd.texture.get_pixel_value` 都不应再静默回退到别的 event。

如果后续要把资源追踪结果再喂回事件链路，请额外注意：

- `rd.resource.get_usage` / `rd.resource.get_history` 中只有 canonical `event_id` 可以直接用于 `rd.event.*`。
- `raw_event_id` 仅用于诊断底层记录；当 `event_resolvable=false` 时，不应把它直接传给 `rd.event.set_active`。

## 5. 进一步阅读

- 想理解这些状态对象的关系：见 [session-model.md](session-model.md)
- 想给上层 Agent / framework 写平台说明：见 [agent-model.md](agent-model.md)
- 想查故障恢复：见 [troubleshooting.md](troubleshooting.md)
