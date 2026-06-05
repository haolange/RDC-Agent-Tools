# Quickstart

Run all commands from `resources/tools`, or set `RDX_TOOLS_ROOT` when using `bin/rdx` from another directory.

```bat
rdx.bat --version
rdx.bat version --json
rdx.bat --json doctor
rdx.bat tools search pipeline --json
rdx.bat context status --json
rdx.bat capture open --file "C:\path\sample.rdc" --frame-index 0
rdx.bat context status --json
rdx.bat context update --key notes --value "triaged" --json
rdx.bat vfs ls --path / --format tsv
rdx.bat vfs tree --path / --depth 2 --format json
rdx.bat completion powershell
```

For preview checks after a capture is open:

```bat
rdx.bat session preview on
rdx.bat session preview status
rdx.bat session preview off
```

Inspect `preview.display` in `context status` JSON output for framebuffer, window, and fit geometry. Use `context clear` and `daemon stop` at the end of smoke runs.

JSON is the canonical agent protocol. TSV is only a tabular projection for list/navigation commands such as `vfs ls`; nested state such as context, pipeline, shaders, and preview remains JSON.

For agent-visible smoke, run the bash entrypoint instead of a Python smoke runner:

```bash
bash scripts/smoke_cli.sh
bash scripts/smoke_cli.sh --skip-rdc
bash scripts/smoke_cli.sh --rdc "C:/path/sample.rdc" --context cli-smoke
```

The script prints each CLI command before executing it and mirrors output to `intermediate/logs/smoke_cli.log`. Without `--rdc`, it uses the first `tests/fixtures/*.rdc` fixture when one exists. If a daemon-backed command times out, it prints the failed command, daemon status, known context state fields, and cleanup results.

Remote-only smoke still uses CLI transport. Watch for `remote_handle_consumed` after `rd.capture.open_replay` binds a remote handle to a session.
