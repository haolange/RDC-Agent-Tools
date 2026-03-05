# 配置

## 必需的运行时布局（runtime layout）

- `binaries/windows/x64/renderdoc.dll`
- `binaries/windows/x64/renderdoc.json`
- `binaries/windows/x64/pymodules/renderdoc.pyd`

## 环境变量（environment variables）

- `RDX_TOOLS_ROOT`：可选；从 launcher scripts 自动探测。
- `RDX_RENDERDOC_PATH`：可选；用于覆盖 `renderdoc.pyd` 目录路径。
- `RDX_ARTIFACT_DIR`：可选；用于覆盖 artifact 输出根目录。
- `RDX_LOG_LEVEL`：可选；用于设置 logging level。

默认输出位于 `intermediate/`。