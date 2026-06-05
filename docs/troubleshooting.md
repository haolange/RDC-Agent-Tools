# Troubleshooting

## Doctor

Run `rdx.bat --json doctor` first. It reports the tools root, Python runtime, RenderDoc DLL/PYD layout, catalog count, launchers, daemon status, and `mcp.supported=false`.

## Session State

Use `rdx.bat context status --json` to inspect the active context. If the wrong capture/session is active, run `rdx.bat context clear --json` and reopen the `.rdc`. Use `rdx.bat context update --key notes --value "..." --json` to leave agent-facing recovery notes.

If VFS, `diff pipeline`, or `assert pipeline` reports `session_required`, the selected `--daemon-context` has no active session. Open a capture with `capture open --file <rdc>` or pass `--session-id`.

## Remote Lifecycle

If a remote replay fails after `rd.remote.connect`, check whether state says `remote_handle_consumed`. That means the handle was consumed by `rd.capture.open_replay`; reconnect or recover through the remote workflow instead of reusing the old handle.

## Preview

preview 打不开或自动失效：先检查 `session preview status` and the current `session_id` from `context status`.

preview 看着不全、留黑边或像是畸形：检查 `preview.display` and confirm the framebuffer extent is not being confused with viewport / scissor.
