# Agent Model

Agents should treat `rdx-tools` as CLI-only. Use the `rdx` command through the agent shell tool.

Read state with `rdx context status` before acting, and write scoped notes or focus with `rdx context update`. `session_locator` summarizes the active capture/session/event for the selected daemon context. Multiple daemon contexts are isolated from each other, and the caller chooses which context id to use.

Use VFS as the runtime exploration layer: start with `rdx vfs ls --path / --format tsv`, then read `rdx vfs cat --path /context --format json`, and only run bounded trees such as `rdx vfs tree --path /draws --depth 2 --max-nodes 2000 --format json`. Broad `/draws` tree nodes are summaries; when a node reports `detail_deferred=true`, use `event show`, targeted `vfs cat`, or the canonical `rd.event.get_action_details` tool for that event. Do not broad-expand `/resources`, `/textures`, or `/buffers`; use `vfs ls` and targeted `vfs cat` or canonical `rd.*` tools. JSON is canonical; TSV is only for tabular navigation and list projections.

When remote state reports `remote_handle_consumed`, do not reuse that remote handle. Use the remote lifecycle tools to reconnect, ping, or reopen replay as needed.

For shader edits, always read `edit_plan` from `rd.shader.get_source`, `rd.shader.get_disassembly`, `rd.shader.compile`, or `rd.shader.edit_and_replace` before deciding how to modify text. If debug source is unavailable, `edit_plan.recommended_next_tool` tells the agent whether to inspect IR through `rd.shader.get_disassembly` or export a raw container through `rd.shader.extract_binary`. SPIR-V ASM can be text-editable when the plan allows it. DXIL/DXBC disassembly is captured_source_editable=false and must not be edited as text; full replacement is a separate path where the user provides complete HLSL/GLSL through `source_text` or `source_path`, plus `entry` and `target`, and the runtime reports `runtime_full_replace_supported`.

`source_target` names the representation returned by RenderDoc, such as `SPIR-V ASM` or `DXIL`. `source_encoding` names how replacement input will be interpreted by the replay backend. These are related but not interchangeable; use the values from `edit_plan` and returned tool payloads.

Agents use `rd.session.open_preview` through CLI commands when a human observer needs the preview window. `preview.display` is the stable state surface for window and framebuffer geometry.

For the task-level rdx-native SOP, use [rdx-native agent playbook](rdx-native-agent-playbook.md). For exact catalog coverage, use [Tool reference](tool-reference.md), generated from `spec/tool_catalog.json`.
