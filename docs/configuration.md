# 配置

本文聚焦 `rdx-tools` 的 runtime layout、环境变量、artifact 输出与根目录约束，不重复介绍 `CLI` / `MCP` 的入口用法。

## 1. 运行时布局

`binaries/windows/x64` 必须包含：

- `renderdoc.dll`
- `renderdoc.json`
- `pymodules/renderdoc.pyd`

这些文件是运行时前置条件，不属于可选示例资源。

## 2. 参考根目录

默认情况下，参考根目录由 `rdx.bat` 或脚本自身位置推导。

- `RDX_TOOLS_ROOT`
  - 可选。
  - 仅用于覆盖默认参考根目录。
- 禁止依赖父目录逃逸或硬编码仓库外绝对路径作为运行时逻辑。

## 3. 环境变量

- `RDX_TOOLS_ROOT`
  - 覆盖默认参考根目录。
- `RDX_RENDERDOC_PATH`
  - 可选；覆盖 `renderdoc.pyd` 目录路径。
- `RDX_ARTIFACT_DIR`
  - 可选；覆盖 artifact 输出根目录。
- `RDX_LOG_LEVEL`
  - 可选；设置日志级别。
- `RDX_DATA_DIR`
  - 可选；为运行时数据目录提供显式覆盖。

## 4. artifact 输出

默认输出位于 `intermediate/`，重点包括：

- `intermediate/runtime/rdx_cli/`
- `intermediate/artifacts/`
- `intermediate/pytest/`
- `intermediate/logs/`

`intermediate/` 内容属于运行时产物，应视为非源码材料。

## 5. preview 运行约束

- preview 只服务 Windows 本地可视化监控，不额外引入跨平台 UI 抽象。
- preview 是 worker-owned 的人类观察窗口，不是 artifact，也不是新的持久化对象类型。
- `rd.session.get_context.preview` 只暴露状态，不额外引入 `preview_id` 或第二套资源命名空间。
- preview 默认显示完整 framebuffer / 当前 RT，不按 viewport 裁小；若当前 event 存在 viewport / scissor，会在完整 framebuffer 上做区域标识。
- `rd.session.get_context.preview.display.fit_mode` 固定为 `fit_with_screen_cap`，`screen_cap_ratio` 当前固定为 `0.5`。
- preview 窗口会按 framebuffer 几何自动调整大小，但默认上限不超过当前屏幕工作区的 `50%`。
- 若用户手动拖拽过窗口，在 framebuffer 几何不变时不会被持续抢改；只有首次打开、session 改变或 framebuffer 几何变化时才会重新套用默认窗口尺寸。
- `rdx daemon stop` / worker 重启会关闭 live preview 窗口，但默认保留该 context 的 preview enabled intent。
- `rd.session.close_preview`、`rd.core.shutdown` 与 `rdx context clear` 会关闭窗口并清掉该 intent。

## 6. 文档维护检查

文档变更后，建议至少执行：

```bat
python scripts/check_markdown_health.py
```

该检查面向：

- `*.md` 编码是否为 UTF-8 with BOM
- 关键文档是否存在
- 本地 Markdown 链接是否悬空
## 新增运行时配置

- `snapshot_retention.total_limit`
  - context snapshot 中 `last_artifacts` 的全局上限，默认 `32`。
- `snapshot_retention.per_type_limit`
  - context snapshot 中同一 `artifact.type` 的上限，默认 `8`。
- `confidence_weights`
  - `sharpness` / `consistency` / `range_factor` 三项 bisect 置信度权重；权重在写入时会归一化。
- `adaptive_bisect.mode`
  - `off | recommend`；首版只支持离线推荐，不做在线自动写回。
