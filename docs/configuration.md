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

## 5. 文档维护检查

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
