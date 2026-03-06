# Troubleshooting

## `rdx.bat` 双击后窗口像是“闪退”

当前预期行为是进入交互式菜单，而不是直接打印帮助后退出。

如果没有进入菜单，优先检查：

- `rdx.bat` 是否能找到 `scripts/rdx_bat_launcher.ps1`
- 当前目录是否是 `rdx-tools` 根目录
- `RDX_TOOLS_ROOT` 是否被外部环境错误覆盖

## `Start CLI` 退出后 daemon 还在

这是预期行为。

- `exit` / `quit`
  - 只退出当前 shell
  - 默认保留 daemon 与 context

如果需要显式停止或清理，请使用：

```bat
rdx daemon status
rdx daemon stop
rdx context clear
```

## 状态面与来源优先级是什么

排查问题时，请先区分三类状态面：

- local session state
  - 由 `capture open` 等命令写入本地状态文件，供后续命令读取。
- daemon state
  - 记录 daemon 生命周期、context、pipe、已附着 client、部分会话摘要。
- runtime 内部对象
  - 真正的 replay、debug、active event、controller 等进程内对象。

因此：

- `capture status` 读的是 local session state，不是直接探测 live runtime。
- `daemon status` 读的是 daemon state，不等价于 local session state，也不保证字段完全同构。
- 状态文件缺失，不一定等于 runtime 已完全不可用；反过来，daemon 还在，也不等于 local session state 一定存在。

## `rdx daemon status` 返回 `no active daemon`

常见原因：

- 该 context 从未启动 daemon
- daemon 已被显式 `stop`
- daemon 因无 attached client 且超过 idle TTL 自动退出
- state file 已被 stale cleanup 清理

恢复方式：

- 重新进入 `Start CLI` 或 `Start MCP`
- 或显式执行 `rdx daemon start`
- 再重新打开 capture 或恢复上层调用链路

## 为什么 `capture status` 没有 session，但 `daemon status` 还显示 daemon 在

这是可能发生的。

原因通常不是“平台自相矛盾”，而是因为两条命令读取的是不同状态面：

- `capture status` 读取 local session state
- `daemon status` 读取 daemon state

可能场景包括：

- daemon 仍然存活，但 local session state 尚未写入或已被清理
- daemon 仍然存活，但当前 context 下没有可复用的 capture/session 摘要
- 你查询的是不同 context

如果目标是继续使用当前 `.rdc` 链路，优先检查当前 context，并视情况重新执行 `capture open`。

## 为什么有 local session state，但 daemon 没有附着或没有 active session 摘要

这也是可能发生的。

因为 local session state 与 daemon state 不是同一份数据：

- 前者更像“本地命令读取入口”
- 后者更像“daemon 生命周期与共享状态入口”

如果你是非 daemon 直连路径建立的 session，local session state 可以存在，而 daemon 并没有对应附着关系。

## 顺序执行有效，为什么并发观测可能读不到状态

文档中的示例默认按顺序执行语义编写。

如果你把 `capture open` 和 `capture status` 并发执行，`capture status` 可能在状态文件尚未写入完成前就读取，从而得到“没有 session state”的结果。

这不应被上升为平台定义。除非文档明确声明支持并发，否则请按顺序链路理解文档示例。

## 什么时候应该重开 `.rdc`，什么时候只需要重连 daemon / 复用 context

优先按下面的思路判断：

- daemon 不在了
  - 先重连或重启 daemon。
- daemon 在，但 local session state 缺失
  - 先检查 context 是否一致；若只是本地状态缺失，通常重新 `capture open` 最直接。
- local session state 在，但后续操作失败
  - 说明问题可能在 runtime 内部对象层；上层可以选择重建 session，而不必立即怀疑 catalog 或契约。

## `CLI` 中 `--daemon-context` 放哪里

`--daemon-context` 是顶层参数，必须放在子命令前：

```bat
python cli/run_cli.py --daemon-context smoke daemon status
```

而不是：

```bat
python cli/run_cli.py daemon status --daemon-context smoke
```

后者会被 argparse 识别为非法参数位置。

## PowerShell 与 `cmd` 下 `--args-json` 写法不同

这是命令行解析差异，不是 `rdx-tools` 独有问题。

在 `cmd` / `CLI shell` 中常见写法是：

```bat
rdx call rd.event.get_actions --args-json "{\"session_id\":\"<session_id>\"}" --json --connect
```

如果你在 PowerShell 里直接调用 `python cli/run_cli.py ...`，需要根据 PowerShell 的转义规则重新组织 JSON 字符串。优先建议：

- 先使用 `rdx.bat` 的 `Start CLI`
- 或把复杂 JSON 放入脚本变量后再传参

## `Start MCP` 里的 `stdio` 为什么没有 URL

这是预期行为。

`stdio` transport 不提供网络端点，因此 launcher 会显示“无 URL”或等价提示。

如果你需要网络端点，请选择 `streamable-http`。

## `streamable-http` 启动失败

优先检查：

- `host` / `port` 是否可用
- 当前机器是否已有其他进程占用同一端口
- daemon 是否已成功启动
- `python mcp/run_mcp.py --help` 与 `python mcp/run_mcp.py --ensure-env` 是否可运行

## shell 异常关闭后会不会留下 daemon

`rdx-tools` 已实现：

- `attached_clients`
- lease / heartbeat
- idle TTL
- stale state cleanup

短时间误关 shell 后，通常仍可在相同 context 上重新附着。

长时间无人接管时，daemon 会因无 attached client 且超过 idle TTL 自动退出。

## `rd.remote.connect` 成功了，但后续 `rd.capture.open_replay(options.remote_id=...)` 仍然失败

先区分两类问题：

- `rd.remote.connect` / `rd.remote.ping` 本身失败
  - 这说明 live endpoint 没建起来，不应继续使用该 `remote_id`。
- `rd.remote.connect` / `rd.remote.ping` 成功，但 `open_replay` 失败
  - 优先检查 remote endpoint 本身、样本兼容性、以及 remote host 是否真的有可用 replay 环境。

对 Android remote，`rd.remote.connect` 会负责 `adb` bootstrap；因此如果你是通过 `rdx-tools` 入口复现问题，不应再把“先手工开 `qrenderdoc`”当成默认前置。

## Android remote 常见失败面

优先检查：

- `adb devices -l` 是否只有一个 `device`，或你是否显式传了 `options.device_serial`
- 仓库内 APK 是否存在：`binaries/android/arm32/`、`binaries/android/arm64/`
- `rd.remote.connect` 的 `options.transport` 是否设为 `adb_android`
- `rd.remote.ping` 是否成功
- `adb forward --list` 中是否看到了本次链路创建的本地端口

如果 `rd.remote.connect` 失败，先修它；不要继续把依赖 `remote_id` 的后续报错误判成 replay 层问题。