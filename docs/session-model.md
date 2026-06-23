# Session Model

The CLI runtime stores context state per daemon context. `rdx context status` returns the current state; `rdx context update` changes agent-facing fields such as notes and focus. `session_locator` summarizes the active `.rdc`, session, frame, and event.

`--daemon-context <id>` selects the continuous runtime namespace. It is not a daemon-mode switch; omitting it uses `default`. `rdx context list` shows known namespaces and `rdx context clear` clears the selected namespace.

Multiple daemon contexts are isolated from each other, and callers choose the context id they want to operate on. Remote handles still keep context locality and lifecycle protection. `remote_handle_consumed` means the handle has moved into session ownership and reconnect logic must create or recover a fresh handle.

## Replay Lifecycle

`rd.capture.open_replay` is idempotent for a live same-capture session in the same daemon context and returns `reused_session=true` instead of opening a second D3D12 replay. `rd.capture.close_replay` is the canonical resource release path. If stale session cleanup fails, the runtime returns `stale_session_requires_restart` with recovery commands rather than silently creating another replay.

## Preview State

The session context includes `preview` state. Preview geometry must describe the whole framebuffer and distinguish viewport / scissor from preview window fitting. `preview.display` is the stable surface for framebuffer extent, viewport rect, window rect, fit mode, and screen cap settings.
