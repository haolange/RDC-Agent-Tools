# rdx-tools

`rdx-tools` is a CLI-only RenderDoc `.rdc` runtime package. It exposes 200 `rd.*` tools through `rdx.bat`, `bin/rdx`, or `python cli/run_cli.py`; it no longer ships an MCP server, MCP transport, or built-in RDC ToolBridge MCP descriptor.

## Entry Points

```bat
rdx.bat --version
rdx.bat version --json
rdx.bat --json doctor
rdx.bat tools list --json
rdx.bat context status --json
rdx.bat capture open --file "C:\path\capture.rdc" --frame-index 0
rdx.bat context update --key notes --value "triaged" --json
rdx.bat vfs ls --path / --format tsv
rdx.bat completion powershell
```

```bash
bash resources/tools/bin/rdx --json doctor
```

`--non-interactive` is a launcher flag only. `rdx.bat --non-interactive --json doctor` runs the same CLI. `rdx.bat --non-interactive mcp --ensure-env` is intentionally unsupported and returns non-zero JSON.

## Smoke

Agent platforms should run smoke through bash so every CLI step is visible in the terminal:

```bash
bash scripts/smoke_cli.sh
bash scripts/smoke_cli.sh --rdc "C:/path/sample.rdc" --context cli-smoke
```

The smoke script calls `bin/rdx` directly for `doctor`, `tools list`, `tools search`, the negative MCP route check, and the daemon-backed capture chain. If `--rdc` is omitted, it uses the first `tests/fixtures/*.rdc` fixture when one exists. It writes the same live output to `intermediate/logs/smoke_cli.log`. It does not run a Python smoke runner or a Python command aggregator.

## Install

Release packages are self-contained Windows x64 zips. See [Install](docs/install.md).

## Session State

Use `rdx context status` to read context state and `rdx context update` to update notes, focus, and agent-visible metadata. `--daemon-context <id>` selects the continuous runtime namespace; omitting it uses `default`. The state includes `session_locator`, current capture/session IDs, preview state, and remote lifecycle fields. `remote_handle_consumed` means a remote handle has been bound to a replay session and must not be reused as a free remote connection.

## Preview CLI Contract

`session preview on|status|off` is daemon-backed. `rdx context status` reports preview state and `preview.display`; the preview surface should expose the complete framebuffer（完整 framebuffer）instead of cropping viewport / scissor state.

## Docs

- [Session model](docs/session-model.md)
- [Agent model](docs/agent-model.md)
- [Install](docs/install.md)
- [Agent integration](docs/agent-integration.md)
- [Stability](docs/stability.md)
- [Documentation governance](docs/doc-governance.md)
- [Tools](docs/tools.md)
- [Scripts](scripts/README.md)
