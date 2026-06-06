# Agent Integration

Agents should call `rdx-tools` through their shell tool. The public entrypoints are:

- `rdx.bat`
- `bin/rdx`
- `python cli/run_cli.py`

Recommended probes:

```bat
rdx.bat --version
rdx.bat --json doctor
rdx.bat context status --json
rdx.bat tools search pipeline --json
rdx.bat vfs ls --path / --format tsv
```

Canonical agent lifecycle:

```bat
rdx.bat --daemon-context task-123 --json doctor
rdx.bat --daemon-context task-123 context status --json
rdx.bat --daemon-context task-123 capture open --file "C:\captures\case.rdc"
rdx.bat --daemon-context task-123 vfs tree --path / --depth 2 --format json
rdx.bat --daemon-context task-123 context update --key notes --value "triaged" --json
rdx.bat --daemon-context task-123 context clear --json
rdx.bat --daemon-context task-123 daemon stop
```

After enabling preview for an opened capture, agents should inspect `preview.display` in `context status --json` for framebuffer, window, and fit geometry instead of inferring geometry from screenshots alone.

Shader edit/replace tools expose an `edit_plan` object in JSON payloads. Agents should treat it as the machine-readable usage contract: it says whether the current shader text can be edited, which edit inputs are allowed, which patch ops are safe, whether a toolchain such as `spirv-as` is required, and which tool to call next when replacement is unsupported. A `can_replace=false` plan is a safe stop, not a signal to retry with a different text encoding.

`--daemon-context <id>` selects a continuous runtime namespace. It is not a daemon-mode switch; omitting it uses the `default` namespace. JSON is the canonical protocol. TSV is an optional tabular projection for list/navigation commands; use JSON for nested runtime state.

For visible smoke, use bash so every CLI command and result appears in the agent terminal:

```bash
bash scripts/smoke_cli.sh
```

`rdx-tools` is CLI-only. Agents should integrate through shell commands and the canonical JSON envelope.

