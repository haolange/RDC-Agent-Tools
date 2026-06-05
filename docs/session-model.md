# Session Model

The CLI runtime stores context state per daemon context. `rdx context status` returns the current state; `rdx context update` changes agent-facing fields such as notes and focus. `session_locator` identifies the active `.rdc`, session, frame, and event for staged_handoff between agents.

`--daemon-context <id>` selects the continuous runtime namespace. It is not a daemon-mode switch; omitting it uses `default`. `rdx context list` shows known namespaces and `rdx context clear` clears the selected namespace.

Local contexts support orchestrated multi-context work. Remote contexts are stricter because remote handles can be consumed by live replay sessions. `remote_handle_consumed` means the handle has moved into session ownership and reconnect logic must create or recover a fresh handle.

## Preview State

The session context includes `preview` state. Preview geometry must describe the whole framebuffer and distinguish viewport / scissor from preview window fitting. `preview.display` is the stable surface for framebuffer extent, viewport rect, window rect, fit mode, and screen cap settings.
