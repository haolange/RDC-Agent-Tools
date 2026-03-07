# Quickstart

本文只覆盖“最短上手路径”，帮助你确认 `rdx-tools` 的入口可用，并把一份 `.rdc` 变成可操作的 session。更完整的状态模型见 [session-model.md](session-model.md)。

## 1. 先验证入口

在仓库根目录执行：

```bat
python cli/run_cli.py --help
python mcp/run_mcp.py --help
python spec/validate_catalog.py
```

如果以上命令都可运行，再继续下面的 `CLI` 或 `MCP` 路径。

## 2. 人工使用 `CLI`

最方便的人工入口是：

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
rdx capture open --file "C:\path\capture.rdc" --frame-index 0 --connect
rdx capture status
rdx call rd.event.get_actions --args-json "{\"session_id\":\"<session_id>\"}" --json --connect
rdx daemon status
```

你会得到至少这些关键信息：

- `capture_file_id`
- `session_id`
- `active_event_id`

其中 `active_event_id` 只会写成可被 `rd.event.get_action_details` round-trip 的 action event。若 `rd.event.set_active` 收到不可解析的 `event_id`，调用会失败，且不会污染当前 context。

如果后续要把同一条链路交给上层 Agent 继续使用，建议额外查看：

```bat
rdx call rd.session.get_context --json --connect
```

### 结束与清理

```bat
rdx daemon stop
rdx context clear
```

`exit` / `quit` 只退出当前 shell，不会自动停止 daemon，也不会自动清理 context。

## 3. 对接 `MCP` client

`MCP` 入口适合被外部 `MCP` client 或 Agent 接入。你可以通过 launcher 启动，也可以直接用脚本启动。

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

## 4. `MCP` 最小工具链路

对接 `MCP` client 后，建议先完成这条平台级最小链路：

1. `rd.core.init`
2. `rd.capture.open_file`
3. `rd.capture.open_replay`
4. `rd.replay.set_frame`
5. `rd.event.get_actions`
6. 需要确认当前 context 状态时，调用 `rd.session.get_context`

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

如果后续要把资源追踪结果再喂回事件链路，请额外注意：

- `rd.resource.get_usage` / `rd.resource.get_history` 中只有 canonical `event_id` 可以直接用于 `rd.event.*`。
- `raw_event_id` 仅用于诊断底层记录；当 `event_resolvable=false` 时，不应把它直接传给 `rd.event.set_active`。

## 5. 进一步阅读

- 想理解这些状态对象的关系：见 [session-model.md](session-model.md)
- 想给上层 Agent / framework 写平台说明：见 [agent-model.md](agent-model.md)
- 想查故障恢复：见 [troubleshooting.md](troubleshooting.md)
