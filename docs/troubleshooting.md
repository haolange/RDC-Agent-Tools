# Troubleshooting

## Doctor

Run `rdx --json doctor` first. It reports the tools root, Python runtime, RenderDoc DLL/PYD layout, catalog count, launchers, and daemon status.

## Session State

Use `rdx context status --json` to inspect the active context. If the wrong capture/session is active, run `rdx context clear --json` and reopen the `.rdc`. Use `rdx context update --key notes --value "..." --json` to leave agent-facing recovery notes.

If VFS, facade commands, `diff pipeline`, or `assert pipeline` reports `session_required`, the selected `--daemon-context` has no active session. Open a capture with `capture open --file <rdc>` or pass `--session-id`.

## Remote Lifecycle

If a remote replay fails after `rd.remote.connect`, check whether state says `remote_handle_consumed`. That means the handle was consumed by `rd.capture.open_replay`; reconnect or recover through the remote workflow instead of reusing the old handle.

## Replay Reopen

`rd.capture.open_replay` reuses an existing live session for the same capture in the same context. Use `rd.capture.close_replay` to release replay resources before reopening. After a daemon timeout, check `rdx context status --json` and operation history before clearing or stopping the context. If stale cleanup fails, the runtime returns `stale_session_requires_restart` with recovery commands.

## Shader Replacement

Read `edit_plan` before editing shader text. It is returned by `rd.shader.get_source`, `rd.shader.get_disassembly`, `rd.shader.compile`, and `rd.shader.edit_and_replace`.

When debug source is unavailable, `rd.shader.get_source` returns a format-aware fallback. SPIR-V can point to `rd.shader.get_disassembly` with `target=SPIR-V ASM` and `source_encoding=spirvasm`. If `rd.shader.edit_and_replace` edits raw SPIR-V ASM and the replay backend only accepts binary `SPIRV`, `rdx-tools` uses `spirv-as` to assemble the edited ASM before calling RenderDoc. Check `rdx --json doctor` -> `shader_tools.spirv_as` when the tool returns `shader_build_failed` with `failure_reason=spirv_assembly_failed`.

DXIL/DXBC disassembly is read-only by default. If `edit_plan.captured_source_editable=false`, do not pass that disassembly text to `rd.shader.edit_and_replace`. Full replacement is separate: provide complete HLSL/GLSL through `source_path` or `source_text`, plus `entry` and `target` such as `ps_6_6`. Build failures do not call `ReplaceResource`; replacement failures include `replacement_attempted`, `cleanup_attempted`, and `context_preserved`.

## Texture Export

`rd.export.texture` uses `file_format` as the canonical parameter. The boundary alias `format` is accepted only for legacy callers and is reported in `deprecated_alias_used`. Explicit HDR/EXR/DDS requests fail closed when the runtime cannot produce that format; PNG is display-mapped output and should not be treated as HDR data evidence.

## Preview

preview 打不开或自动失效：先检查 `session preview status` and the current `session_id` from `context status`.

preview 看着不全、留黑边或像是畸形：检查 `preview.display` and confirm the framebuffer extent is not being confused with viewport / scissor.

## Facade TSV

`--format tsv` is supported only by list/projection commands such as `vfs ls`, `event list`, and `resource list`. Use JSON for nested state such as `pipeline show`, `shader disasm`, `export screenshot`, `pixel history`, and resource details.
