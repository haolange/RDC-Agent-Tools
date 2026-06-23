# Quickstart

Run all commands from `resources/tools`, or set `RDX_TOOLS_ROOT` when using `bin/rdx` from another directory.

```bat
rdx --version
rdx version --json
rdx --json doctor
rdx tools search pipeline --json
rdx context status --json
rdx capture open --file "C:\path\sample.rdc" --frame-index 0
rdx context status --json
rdx context update --key notes --value "triaged" --json
rdx vfs ls --path / --format tsv
rdx vfs tree --path / --depth 2 --format json
rdx event list --format tsv
rdx pipeline show --event-id 42 --format json
rdx resource list --format tsv
rdx completion powershell
```

For preview checks after a capture is open:

```bat
rdx session preview on
rdx session preview status
rdx session preview off
```

Inspect `preview.display` in `context status` JSON output for framebuffer, window, and fit geometry. Use `context clear` and `daemon stop` at the end of smoke runs.

JSON is the canonical agent protocol. TSV is only a tabular projection for list/navigation commands such as `vfs ls`, `event list`, and `resource list`; nested state such as context, pipeline, shaders, exports, pixel history, and preview remains JSON.

For agent-visible smoke, run the bash entrypoint so every CLI command is visible:

```bash
bash scripts/smoke_cli.sh
bash scripts/smoke_cli.sh --rdc "C:/path/sample.rdc" --context cli-smoke
```

The script prints each CLI command before executing it and mirrors output to `intermediate/logs/smoke_cli.log`. Without `--rdc`, it runs entry smoke only. Pass an explicit `.rdc` path for the daemon-backed capture chain; committed fixtures are for repository tests and are excluded from release packages. If a daemon-backed command times out, it prints the failed command, daemon status, known context state fields, and cleanup results.

Remote-only smoke still uses CLI transport. Watch for `remote_handle_consumed` after `rd.capture.open_replay` binds a remote handle to a session.

For task-level recipes, see [rdx-native agent playbook](rdx-native-agent-playbook.md). The full generated tool list is [Tool reference](tool-reference.md).
