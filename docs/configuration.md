# Configuration

## User configuration

For external users, prefer the self-contained Windows x64 release package and `scripts/rdx_install.ps1`. The release package runs without installing Python, creating a virtual environment, running a package manager, or carrying a dependency lock file.

Set `RDX_TOOLS_ROOT` only when launching `bin/rdx` from another directory and the tools root cannot be inferred from the entrypoint location.

## Maintainer configuration

`RDX_PYTHON` is a maintainer/debug override for Python selection. It is not part of the user install path and should not be required by a GA release package.

Runtime artifacts live under `intermediate/runtime`, `intermediate/artifacts`, and `intermediate/logs` during development, tests, and release validation.

## preview 运行约束

Preview uses `screen_cap_ratio` to keep the preview window bounded while preserving framebuffer geometry.
