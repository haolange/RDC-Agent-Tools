# Agent Model

Agents should treat `rdx-tools` as CLI-only. Use `rdx.bat`, `bin/rdx`, or `python cli/run_cli.py`; do not expect an MCP server from this package.

Read state with `rdx context status` before acting, and write scoped notes or focus with `rdx context update`. Keep `session_locator` in handoff messages so another agent can resume the same capture/session/event. Local backends allow orchestrated multi-context work; remote backends require stricter ownership checks.

Use VFS as the runtime exploration layer: start with `rdx vfs ls --path / --format tsv`, then use `rdx vfs tree --path / --depth 2 --format json` or `rdx vfs cat --path /context --format json` before choosing precise `rd.*` tools. JSON is canonical; TSV is only for tabular navigation and list projections.

When remote state reports `remote_handle_consumed`, do not reuse that remote handle. Use the remote lifecycle tools to reconnect, ping, or reopen replay as needed.

Agents use `rd.session.open_preview` through CLI commands when a human observer needs the preview window. `preview.display` is the stable state surface for window and framebuffer geometry.
