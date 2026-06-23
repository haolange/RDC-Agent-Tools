# Stability

`rdx-tools` 1.x treats the CLI entrypoints and canonical JSON envelope as stable.

Stable public command:

- `rdx`

Packaged launcher files:

- `rdx.bat`
- `bin/rdx`
- `python cli/run_cli.py`

Stable agent-facing commands:

- `context status|update|list|clear`
- `vfs ls|cat|tree|resolve`
- `diff pipeline|image`
- `assert pipeline|image`

Stable JSON envelope fields:

- `schema_version`
- `tool_version`
- `result_kind`
- `ok`
- `data`
- `artifacts`
- `error`
- `meta`
- `projections`

Exit codes:

- `0`: success
- `1`: runtime, assertion, or tool operation failure
- `2`: argument, setup, installation, or bootstrap failure

Compatibility policy:

- Version 1.0.0 is the first GA release; pre-GA ownership, lease, baton, handoff, and runtime materialization experiments are outside the 1.0 public contract surface.
- 1.x releases may add commands, fields, diagnostics, and tools.
- 1.x releases must not remove published commands or change the canonical JSON envelope semantics.
- Tool-specific JSON `data` payloads may add fields such as shader `edit_plan` when they make agent usage safer without changing existing fields.
- JSON is the canonical agent protocol.
- TSV is a stable tabular projection only where a command documents table output, such as `vfs ls`.
- `--daemon-context <id>` selects a continuous runtime namespace; omitting it uses `default`.
- Breaking changes require a 2.0 release.

