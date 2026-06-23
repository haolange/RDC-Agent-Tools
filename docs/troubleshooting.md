# Troubleshooting

## Doctor

Run `rdx --json doctor` first. It reports the tools root, Python runtime, RenderDoc DLL/PYD layout, catalog count, launchers, and daemon status.

## Session State

Use `rdx context status --json` to inspect the active context. If the wrong capture/session is active, run `rdx context clear --json` and reopen the `.rdc`. Use `rdx context update --key notes --value "..." --json` to leave agent-facing recovery notes.

If VFS, `diff pipeline`, or `assert pipeline` reports `session_required`, the selected `--daemon-context` has no active session. Open a capture with `capture open --file <rdc>` or pass `--session-id`.

## Remote Lifecycle

If a remote replay fails after `rd.remote.connect`, check whether state says `remote_handle_consumed`. That means the handle was consumed by `rd.capture.open_replay`; reconnect or recover through the remote workflow instead of reusing the old handle.

## Shader Replacement

Read `edit_plan` before editing shader text. It is returned by `rd.shader.get_source`, `rd.shader.get_disassembly`, `rd.shader.compile`, and `rd.shader.edit_and_replace`.

When debug source is unavailable, `rd.shader.get_source` returns a format-aware fallback. SPIR-V can point to `rd.shader.get_disassembly` with `target=SPIR-V ASM` and `source_encoding=spirvasm`. If `rd.shader.edit_and_replace` edits raw SPIR-V ASM and the replay backend only accepts binary `SPIRV`, `rdx-tools` uses `spirv-as` to assemble the edited ASM before calling RenderDoc. Check `rdx --json doctor` -> `shader_tools.spirv_as` when the tool returns `shader_build_failed` with `failure_reason=spirv_assembly_failed`.

DXIL/DXBC disassembly is read-only by default. If `edit_plan.can_replace=false`, do not pass that disassembly text to `rd.shader.edit_and_replace`; use debug HLSL source if available, or use `rd.shader.extract_binary` for inspection of the raw container. Unsupported shader formats fail before `BuildTargetShader` or `ReplaceResource`, and the error details include `edit_plan`, `replacement_attempted=false`, and `context_preserved=true`.

## Preview

preview 打不开或自动失效：先检查 `session preview status` and the current `session_id` from `context status`.

preview 看着不全、留黑边或像是畸形：检查 `preview.display` and confirm the framebuffer extent is not being confused with viewport / scissor.
