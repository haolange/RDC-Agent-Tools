# Tool Catalog

`rdx-tools` 暴露 196 个规范的 `rd.*` tools。

本文只承担 catalog 入口职责，不承担使用教程。如何建立 session、如何理解 `context` 与 daemon、如何给 Agent 写平台说明，请分别参考：

- [session-model.md](session-model.md)
- [agent-model.md](agent-model.md)
- [quickstart.md](quickstart.md)

## 规范源说明

理解本仓库时，请记住：

- `spec/tool_catalog_196.json` 与共享响应契约是 tool 能力面与参数语义的规范源。
- runtime 行为是平台真相的运行时体现。
- `CLI` 只是部分平台动作的 convenience wrapper，不是完整能力面的等价镜像，也不是规范源。

## 权威来源

- `spec/tool_catalog_196.json`

## Remote 说明

- `rd.remote.connect` 现在支持可选 `options`，用于选择 `renderdoc` 或 `adb_android` transport，以及 Android 设备 / 本地转发参数。
- `rd.remote.connect` 返回的 `remote_id` 代表 live remote connection；若连接失败，不会返回占位 handle。
- `rd.capture.open_replay` 的 remote 入口是 `options.remote_id`，而不是隐式回退到 `localhost`。
## 校验

```bat
python spec/validate_catalog.py
```
