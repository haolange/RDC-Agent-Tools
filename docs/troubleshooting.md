# 故障排查

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

补充语义：

- `rdx daemon stop`
  - 只停止 daemon。
  - 默认保留本地 `.rdc` 的持久化 session/capture 索引，便于后续 warm resume。
- `rdx context clear`
  - 会显式销毁当前 context 的 snapshot 与持久化恢复状态。
- 若当前 context 已开启 preview：
  - `rdx daemon stop` 会关闭 live 窗口，但默认保留 preview enabled intent，后续同一 context 恢复 live session 时会自动重绑。
  - `rdx context clear` 会关闭窗口并清掉该 intent。

## `scripts/` 主链

优先使用正式支持的脚本主链：`scripts/check_markdown_health.py`、`scripts/release_gate.py`、`scripts/rdx_bat_command_smoke.py`、`scripts/tool_contract_check.py`、`scripts/tool_contract_remote_smoke.py`、`scripts/preview_geometry_smoke.py`、`scripts/smoke_report_aggregator.py`、`scripts/package_runtime.py`、`scripts/cleanup_workspace.py`。
不要把一次性调查脚本或个人调试脚本视为受支持的仓库接口。详见 [../scripts/README.md](../scripts/README.md)。

## `rdx.bat --non-interactive` 现在怎么走

当前正式非交互 launcher 入口是：

```bat
rdx.bat --non-interactive cli --help
rdx.bat --non-interactive cli --daemon-context smoke daemon status
rdx.bat --non-interactive mcp --ensure-env
```

旧的 `cli-shell` / `daemon-shell` alias 已移除；如需直接执行本地命令，请走 `cli` passthrough，或直接调用 `python cli/run_cli.py ...`。

补充说明：

- 如果子命令返回 canonical JSON，launcher 会直接输出完整 payload。
- 只有 launcher 自身失败，或子命令没有可解析 JSON 时，才会回退到短状态 JSON。

## 状态面与来源优先级是什么

排查问题时，请先区分四类状态面：

- daemon 状态（daemon state）
  - 记录 daemon 生命周期、context、pipe、已附着 client、部分会话摘要。
- runtime 内部对象
  - 真正的 replay、debug、active event、controller 等进程内对象。
- context 快照（context snapshot）
  - 由 `rd.session.get_context` 暴露的当前 context 快照，汇总 runtime / remote / focus / recent artifacts。
- persistent context state
  - 保存 `captures`、`sessions`、`current_session_id`、`recovery`、`limits` 与 `recent_operations`，用于多 session 选择与本地恢复。

因此：

- `capture status` 读的是 daemon 摘要与 context 视角，不是直接探测全部 live runtime。
- `daemon status` 读的是 daemon state，不等价于 runtime 内部对象，也不保证字段完全同构。
- `rd.session.get_context` 适合排查当前链路视角，但它也不是“所有 runtime 内部对象的完整转储”。
- `rd.session.list_sessions` / `rd.session.resume` 更适合排查“这个 context 还持有哪些本地 session 记录、哪些已经 degraded”。
- `rd.session.get_context.preview` 只表示人类同步观察面的当前状态；它不是结构化证据，也不代表 fix verification 已成立。

## preview 打不开或自动失效

优先检查：

- `rd.session.get_context.preview.enabled`
- `rd.session.get_context.preview.state`
- `rd.session.get_context.preview.last_error`
- `rd.session.get_context.runtime.active_event_id`
- `rd.session.get_context.preview.display`

当前固定语义：

- preview 只跟随当前 context 的 `current_session_id + active_event_id`
- preview 默认显示完整 framebuffer / RT，不按 viewport 裁小
- 若当前 event 存在 viewport / scissor，会在完整 framebuffer 上做区域标识
- preview 不允许 silent fallback
  - 不会悄悄从 remote 退回 local
  - 不会悄悄从 `active_event` 退回 frame-end framebuffer
  - 不会悄悄改成导出图片/轮询截图式伪预览

因此，`preview.state=failed|stale` 时，应优先修 session / event / backend 条件，而不是把窗口输出当成平台主真相。

## preview 看着不全、留黑边或像是畸形

优先检查：

- `rd.session.get_context.preview.display.framebuffer_extent`
- `rd.session.get_context.preview.display.viewport_rect`
- `rd.session.get_context.preview.display.scissor_rect`
- `rd.session.get_context.preview.display.effective_region_rect`
- `rd.session.get_context.preview.display.window_rect`
- `rd.session.get_context.preview.display.fit_mode`

当前固定语义：

- preview 显示的是完整 framebuffer / RT，不是默认按 viewport 裁小后的图。
- 若当前 event 只在 framebuffer 的部分区域写入，窗口上会显示 viewport / scissor 的区域标识；其余区域为空并不一定是 preview 出错。
- 窗口会按 framebuffer 几何自动调整大小，并把默认上限限制在当前屏幕工作区的 `50%`。
- 若用户手动拖拽过窗口，在 framebuffer 几何不变时，后续 event 跟随不会持续把窗口抢回默认尺寸。
- 当 preview 已 enabled 时，`rd.event.set_active`、`rd.replay.set_frame`、`rd.session.select_session` 与 `rd.session.resume` 会在返回前至少尝试同步一次 preview；如果刷新失败，只更新 preview 状态，不回滚当前 session/event/frame 的主真相。

## replay crash 后 preview 变成 `stale` / `reconnecting`

这是预期路径之一。

- preview 是 context 级 enabled intent，不是独立真相对象。
- replay crash 或 session 失效时，旧窗口会被 runtime 主动关闭。
- 若同一 context 后续 `resume` / rebind 成功，preview 会自动重绑，并在 `rd.session.get_context.preview` 中更新：
  - `state`
  - `recovered_from_session_id`
  - `rebind_count`

如果恢复失败，preview 会停在 `failed`，但这不会自动改写 canonical `rd.*` 的错误面。

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

## `rd.texture.get_data` 写出了 `.npz`，但你以为会得到 PNG

这是当前平台的固定语义，不是异常。

- `rd.texture.get_data`
  - 用于数值 readback / container artifact
  - 默认返回 `.npz`
  - 容器内主数组入口固定为 `pixels`
- `rd.export.texture`
  - 才是可直接打开的纹理图片导出入口

如果你传了：

```json
{"output_path":"preview.png"}
```

当前预期是直接返回 validation 失败，而不是偷偷把 `.npz` 写成 `.png` 文件名。

建议排查顺序：

- 需要像素数组、统计或后续数值分析
  - 继续使用 `rd.texture.get_data`
- 需要 PNG / JPG / EXR / DDS 图片
  - 改用 `rd.export.texture`

## daemon timeout 现在看哪里

daemon-backed `CLI` / `MCP` 超时不再只返回裸字符串。

优先检查 `error.details`：

- `operation`
- `context_id`
- `timeout_seconds`
- `active_operation`
- `daemon_state_excerpt`

## `runtime_owner_conflict`

若某个 context 已被 `rd.session.claim_runtime_owner` claim，后续 live `rd.*` 调用必须提供匹配的 `runtime_owner` 与 `owner_lease_id`。否则会返回：

- `error.code = runtime_owner_conflict`

排查顺序：

- 先用 `rd.session.get_context` 确认当前 `runtime_owner` / `owner_lease`
- 若当前 owner 不是你，先走 handoff / baton，而不是直接继续 live 调用
- 若 owner 已无效，再显式 `rd.session.release_runtime_owner` 或重新 claim

## `runtime_baton_invalid`

若跨 agent / 跨轮次恢复 live 调试失败，优先检查：

- `baton_id` 是否存在
- `capture_ref.rdc_path` 是否仍可访问
- `context_id` 是否正确
- `rehydrate_status.last_error`

如果 `active_operation` 非空，优先把它当成当前唯一权威中间状态；不要再只根据 stderr 文本猜测 daemon 卡在哪一步。

## 终端输出因为脏 Unicode 崩溃

当前 `CLI` / launcher / `MCP` launcher 已统一把终端输出切到可逆转义模式。

如果上层 payload 含有非法 surrogate 或宿主终端编码过窄：

- 平台会优先保留内部 Unicode JSON
- 真正写终端时再用 `backslashreplace`
- 因此你应看到转义后的 `\\udxxx`，而不是 `UnicodeEncodeError`

## 为什么 `capture status` 没有 session，但 `daemon status` 还显示 daemon 在

这是可能发生的。

原因通常不是“平台自相矛盾”，而是因为两条命令读取的是不同状态面：

- `capture status` 读取当前 context 视角
- `daemon status` 读取 daemon state

可能场景包括：

- daemon 仍然存活，但当前 context 没有 live runtime session
- daemon 仍然存活，但当前 context 下没有可复用的 capture/session 摘要
- 你查询的是不同 context

如果目标是继续使用当前 `.rdc` 链路，优先检查当前 context，并按顺序查看：

- `rd.session.get_context`
- `rd.session.list_sessions`
- `rd.session.resume`

## `rd.event.set_active` 返回 `event_not_found`

这通常表示你给的 `event_id` 不是 action tree 中可解析的 canonical event。

常见来源：

- 把 `rd.resource.get_usage` / `rd.resource.get_history` 里的 `raw_event_id` 误当成可直接 round-trip 的 `event_id`
- capture 已切换帧或 session 已重建，旧 event 引用不再对应当前 action tree

当前平台语义是：

- `rd.event.set_active` 会失败
- 现有 runtime / context 中的 `active_event_id` 保持不变
- 后续 `pipeline` / `shader` / `pixel_history` 不应建立在这次失败输入上

排查时优先确认：

- `rd.event.get_action_details(event_id=...)` 是否成功
- `rd.resource.get_usage` / `rd.resource.get_history` 返回的是 canonical `event_id` 还是仅供诊断的 `raw_event_id`

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

## `release_gate.py --require-smoke-reports` 为什么会失败

这条门禁现在不再只看报告文件是否存在，而会读取当前 smoke truth：

- `intermediate/logs/rdx_bat_command_smoke.json`
- `intermediate/logs/tool_contract_report.json`

常见失败原因：

- 当前 smoke markdown / json 不完整
- `rdx_bat_command_smoke.py` 仍有 blocker
- `tool_contract_check.py --local-rdc <path> --skip-remote --transport both` 的 MCP 或 daemon 仍有 blocker
- transport payload 里仍有 `fatal_error`

如果当前仓库没有 first-party fixture，请先用显式外部样本顺序执行真实 local-only smoke，再重跑 gate。

## PowerShell 与 `cmd` 下 `--args-json` 写法不同

这是命令行解析差异，不是 `rdx-tools` 独有问题。

在 `cmd` / `CLI shell` 中常见写法是：

```bat
rdx call rd.event.get_actions --args-json "{\"session_id\":\"<session_id>\"}" --json --connect
```

如果你在 PowerShell 里直接调用 `python cli/run_cli.py ...`，需要根据 PowerShell 的转义规则重新组织 JSON 字符串。当前更稳定的跨 shell 入口是 `--args-file`。优先建议：

- 先使用 `rdx.bat` 的 `Start CLI`
- 或把 JSON 写入 UTF-8 文件后，通过 `--args-file <path>` 传参
- 只有在 shell quoting 明确可控时，才继续使用 `--args-json`

例如：

```powershell
$argsFile = Join-Path $PWD "args.json"
Set-Content -LiteralPath $argsFile -Encoding utf8 -Value '{"session_id":"<session_id>"}'
python cli/run_cli.py call rd.session.get_context --args-file $argsFile --format json
```

## `remote_handle_consumed` 是什么

它现在属于少数生命周期异常面，不再是 remote `open_replay` 成功后的默认结果。

它表示：

- 某个 `remote_id` 曾经是 live remote handle
- 当前恢复链路拿到的是旧 tombstone / 旧 snapshot / 显式 consumed 记录，而不是当前 live handle
- 该 handle 已经不再对应可用的 live remote endpoint

因此：

- 正常 remote `open_replay` 成功后，优先看 `rd.session.get_context -> remote.active_session_ids` 是否已挂到 live handle 上
- 如果只是想断开 live remote，但 `active_session_ids` 非空，预期先看到的是 `remote_handle_in_use`
- 只有当你恢复到了旧 tombstone / 旧 snapshot，或显式复用了失效 handle，才应看到 `remote_handle_consumed`
- 如果只是想确认当前链路状态，优先看 `rd.session.get_context`

## `rd.remote.connect` 成功了，但后续 `rd.capture.open_replay(options.remote_id=...)` 仍然失败

先区分三类问题：

- `rd.remote.connect` / `rd.remote.ping` 本身失败
  - 这说明 live endpoint 没建起来，不应继续使用该 `remote_id`。
- `rd.remote.connect` / `rd.remote.ping` 成功，但 `open_replay` 失败
  - 优先检查 remote endpoint 本身、样本兼容性、以及 remote host 是否真的有可用 replay 环境。
  - 如果 `open_replay` 失败，旧 `remote_id` 不应被视为已消费成功。
- `open_replay` 已成功，后续再用旧 `remote_id` 报错
  - 先看 `rd.session.get_context -> remote.active_session_ids` 与 `rd.remote.disconnect` 的返回是否是 `remote_handle_in_use`。
  - 只有当前 state 明确落在旧 tombstone / consumed handle 上时，才把它归类为 `remote_handle_consumed`。

对 Android remote，`rd.remote.connect` 会负责 `adb` bootstrap；因此如果你是通过 `rdx-tools` 入口复现问题，不应再把“先手工开 `qrenderdoc`”当成默认前置。

## Android remote 常见失败面

优先检查：

- `adb devices -l` 是否只有一个 `device`，或你是否显式传了 `options.device_serial`
- 仓库内 APK 是否存在：`binaries/android/arm32/`、`binaries/android/arm64/`
- `rd.remote.connect` 的 `options.transport` 是否设为 `adb_android`
- `rd.remote.ping` 是否成功
- `adb forward --list` 中是否看到了本次链路创建的本地端口

如果 `rd.remote.connect` 失败，先修它；不要继续把依赖 `remote_id` 的后续报错误判成 replay 层问题。

## `tool_contract_check.py` 的 remote 默认值

如果你用正式 smoke 脚本 `python scripts/tool_contract_check.py --local-rdc <...> --remote-rdc <...>` 跑 remote matrix，当前默认 remote branch 会走 Android `adb` bootstrap，也就是：

- `rd.remote.connect(options.transport="adb_android")`

补充说明：

- 只有一台 Android 设备在线时，可以不额外指定 serial。
- 多设备场景下，优先显式设置 `RDX_REMOTE_DEVICE_SERIAL`。
- 如果目标不是 Android `adb` remote，而是裸 `RenderDoc` remote host，请显式设置 `RDX_REMOTE_CONNECT_TRANSPORT=renderdoc`。
- 当桌面 local replay 只是不兼容当前 GPU / extension 时，正式 smoke 应把它归类为 `sample_compatibility`，而不是无限重复 `open_file` 直到触发 capture limit。

## 如何用 `rd.session.get_context` 定位长链状态

当链路较长、你不确定“当前到底在哪个 session / event / remote 生命周期”时，优先执行：

```bat
rdx call rd.session.get_context --json --connect
```

重点看：

- `runtime.session_id`
- `runtime.capture_file_id`
- `runtime.active_event_id`
- `remote.state`
- `remote.origin_remote_id`
- `last_artifacts`

如果需要补充用户视角焦点，而不是改 runtime 自身状态，再用：

```bat
rdx call rd.session.update_context --args-file ".\context-update.json" --json --connect
```

## shell 异常关闭后会不会留下 daemon

`rdx-tools` 已实现：

- `attached_clients`
- lease / heartbeat
- idle TTL
- stale state cleanup

短时间误关 shell 后，通常仍可在相同 context 上重新附着。

长时间无人接管时，daemon 会因无 attached client 且超过 idle TTL 自动退出。

daemon 退出后，本地 `.rdc` session 默认会保留在持久化 context state 中；再次附着同一 context 时，平台会优先尝试自动恢复本地 session。remote session 不会自动重连。
## 长操作静默

daemon 退出后，本地与可恢复 remote session 默认都会保留在持久化 context state 中；再次附着同一 context 时，平台会优先尝试恢复原 `session_id`。只有 remote endpoint 真断开、Android bootstrap 失败或恢复元数据缺失时，remote session 才会显式进入 `degraded` / error。

## remote inspection 中频繁出现 `Unknown session_id`

这不是正常预期。

当前平台语义是：

- 已成功建立的 replay session，在后续普通 inspection 工具调用中会优先做 lazy recovery。
- 如果能恢复，平台应继续复用原 `session_id`，而不是把上层调用打成新的未知句柄。
- 如果不能恢复，错误应明确落在 remote endpoint、bootstrap、恢复元数据或 RenderDoc runtime 状态上，而不是只剩一个裸 `Unknown session_id`。

排查顺序：

- 先看 `rd.session.get_context` / `rd.session.resume` 返回的 `sessions[*].recovery`、`last_error`、`remote` 元数据。
- 再看失败调用的 `error.details`，确认是 endpoint 断开、`remote.OpenCapture(...)`、Android bootstrap 还是恢复元数据缺失。
- 只有在 endpoint 真断开或恢复确实失败时，才重新执行 `rd.remote.connect -> rd.remote.ping -> rd.capture.open_replay`。

## `rd.shader.edit_and_replace` 失败还是成功，怎么判断

现在只看 canonical 结果：

- 成功时，`ok=true`，并返回 `status="applied"`、`replacement_id` 与 `resolved_event_id`。
- backend 不支持 runtime 替换时，会返回显式 capability 错误，例如 `shader_replace_backend_unsupported`。
- 编译、绑定校验或 replay runtime 失败时，会返回显式 runtime/validation 错误。
- `error.code/details` 现在会尽量区分：
  - `shader_build_runtime_error`
  - `shader_build_failed`
  - `shader_replace_apply_failed`
  - 以及绑定/校验类错误
- 编译相关失败会把 `entry_point`、`encoding`、`compile_flags`、`compiler_output` 等诊断信息写进 `error.details`，便于判断是 `BuildTargetShader(...)` 绑定问题，还是 shader 本身编译失败。

不应再把 `mock_applied`、logical replacement 或“看起来成功但没有真正替换”的状态当成成功。

### Android remote / `SPIR-V (RenderDoc)` 的手动 IR patch 经验性排障建议

如果目标是 Android remote Vulkan shader，建议按这个顺序看：

- 先用 `rd.shader.get_disassembly` 确认目标变量在当前 `SPIR-V (RenderDoc)` 文本里是否真的带有 `[[RelaxedPrecision]]`。
- 再用 `rd.shader.edit_and_replace` 的 `messages` 看这次 `force_full_precision` 到底命中了哪几行。
- 如果返回 `status="noop"`，而且 `messages` 里写着 `matched no RelaxedPrecision lines for variables: ...`，说明这个变量在当前反汇编里没有直接可移除的 `RelaxedPrecision` 行；继续盲目回滚或复打同一个 patch 没意义。
- 如果返回 `status="applied"`，但 `messages` 只命中极少数行，不要默认这就等价于“把这个概念变量整体提升到 full precision”；它只说明当前文本 patch 只改到了那几行。
- 对高影响 patch，不要只看单个 hair 像素。应同时取：
  - hair 采样点
  - face / torso / background 采样点
  - `rd.export.screenshot`

### raw `SPIR-V Asm` 工作流什么时候该用

如果你在 `qrenderdoc` 里走的是 `FS -> Edit -> Decompile with spirv-dis -> Apply changes` 这条链，优先改成下面这套 `rdx-tools` 顺序：

- 先用 `rd.shader.get_disassembly(target="SPIR-V ASM")` 拿 raw asm，并记录返回的 `source_hash`。
- 然后在本地按整段 `source_text` 或 unified `diff_text` 做编辑，不要先把精确 raw asm 改写成 `force_full_precision` 一类高层 op。
- 再用 `rd.shader.edit_and_replace(source_text|diff_text, source_target="SPIR-V ASM", source_encoding="spirvasm", expected_source_hash=...)` 应用补丁。
- 如果返回 `shader_source_mismatch`，说明当前 replay session 中的 shader 文本已经变了；应先重新抓取 disassembly，而不是继续复用旧 patch。
- 如果返回 `shader_patch_diff_failed`，说明 unified diff 与当前 shader 文本基线不一致；先对齐 before artifact，再重新生成 diff。
- `emit_patch_artifacts=true` 或 `output_dir=...` 时，运行时会把改前文本、改后文本和 diff 一起导出，便于和手工 `qrenderdoc` patch 逐项比对。
- 对 Android remote Vulkan 样本，raw asm 精确 patch 成功并不等价于“这组 decoration 就是正确修复”。删除单个 `OpDecorate ... RelaxedPrecision` 也可能让多个不相关区域一起掉成 `0`；因此 raw asm bisect 必须配套多点采样与稳定 revert，而不是只看单个像素。

这条 raw asm 编辑链解决的是“精确 IR patch / apply / revert”问题；它不等价于 `qrenderdoc` 主视窗的最终 framebuffer 观察链。若当前 `rd.export.screenshot` 的 frame-end auto target 与 UI 主视图不一致，应单独把它当成 framebuffer 观察问题排查，不要和 raw asm 编辑能力混为一谈。
- 如果多个不相关采样点一起掉成 `0,0,0,0`，应把它判断为“当前 patch 把整个输出面打坏了”，而不是“已经得到正确黑发效果”。

泛化判断规则如下：

- 若 `force_full_precision` 返回 `status="noop"`，说明当前目标变量在现有反汇编文本里没有直接可移除的 `RelaxedPrecision` 行；这时继续对同一变量盲目复打同类 patch 没有意义。
- 若 `force_full_precision` 返回 `status="applied"`，但只命中极少数 `RelaxedPrecision` 行，说明当前 patch 仅改动了局部文本位置，不应直接推断为“相关精度问题已经被正确修复”。
- 若 patch 后多个不相关采样点与截图一起异常变黑或掉成 `0`，应优先判断为“当前 patch 破坏了输出面”，而不是把它当成目标现象已被正确修复。

## `rd.shader.debug_start` 拿到的 event 不可信

当前平台语义是：

- `rd.shader.debug_start` 只应在请求的同一个 event 内成功解析 debug 上下文。
- 返回值会包含 `resolved_event_id` 与 `resolved_context`，用于给上层做交叉核对。
- 如果 backend 只能从别的 event 找到 trace，或者只能构造 synthetic debug，运行时会显式返回 capability/runtime 失败，不会再静默回退成功。
- 失败时优先看 `error.details.failure_stage` / `failure_reason`：
  - `configure_target` / `all_targets_failed`
  - `pixel_history` / `cross_event_only`
  - `debug_pixel` / `invalid_trace`
  - `trace_state` / `debugger_handle_missing`

如果你看到失败，请优先检查：

- 输入的 `event_id` 是否是 canonical action event。
- `error.details.attempts` 里是否已经明确标出 cross-event fallback 被拒绝。
- 当前 replay backend 是否真的支持该类 shader debug。

## 长操作静默

- 若 `rd.remote.connect` 或 `rd.capture.open_replay` 耗时较长，优先读取 daemon `status/get_state` 中的 `active_operation`。
- 若没有 push-style progress，`active_operation.stage` 仍是唯一权威中间状态，不要再依赖日志文本推断。
