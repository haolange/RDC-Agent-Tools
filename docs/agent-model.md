# Agent Model

Agents should treat `rdx-tools` as CLI-only. Use `rdx.bat`, `bin/rdx`, or `python cli/run_cli.py`.

Read state with `rdx context status` before acting, and write scoped notes or focus with `rdx context update`. Keep `session_locator` in staged_handoff messages so another agent can resume the same capture/session/event. Local backends allow orchestrated multi-context work; remote backends require stricter ownership checks.

Use VFS as the runtime exploration layer: start with `rdx vfs ls --path / --format tsv`, then use `rdx vfs tree --path / --depth 2 --format json` or `rdx vfs cat --path /context --format json` before choosing precise `rd.*` tools. JSON is canonical; TSV is only for tabular navigation and list projections.

When remote state reports `remote_handle_consumed`, do not reuse that remote handle. Use the remote lifecycle tools to reconnect, ping, or reopen replay as needed.

For shader edits, always read `edit_plan` from `rd.shader.get_source`, `rd.shader.get_disassembly`, `rd.shader.compile`, or `rd.shader.edit_and_replace` before deciding how to modify text. If debug source is unavailable, `edit_plan.recommended_next_tool` tells the agent whether to inspect IR through `rd.shader.get_disassembly` or export a raw container through `rd.shader.extract_binary`. SPIR-V ASM can be text-editable when the plan allows it; DXIL/DXBC disassembly is read-only unless the plan reports a buildable source path. Do not pass DXIL/DXBC disassembly text to `rd.shader.edit_and_replace`.

`source_target` names the representation returned by RenderDoc, such as `SPIR-V ASM` or `DXIL`. `source_encoding` names how replacement input will be interpreted by the replay backend. These are related but not interchangeable; use the values from `edit_plan` and returned tool payloads.

Agents use `rd.session.open_preview` through CLI commands when a human observer needs the preview window. `preview.display` is the stable state surface for window and framebuffer geometry.
