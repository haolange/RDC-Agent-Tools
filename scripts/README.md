# scripts

`resources/tools/scripts` contains reusable smoke and release checks for the CLI-only package.

Common checks:

```bat
python scripts/check_markdown_health.py
python scripts/generate_tool_reference.py --check
python scripts/package_release.py
python scripts/release_gate.py --require-smoke-reports --require-release-package
```

Smoke checks should be run through bash so every CLI call is visible to the agent terminal:

```bash
bash scripts/smoke_cli.sh
bash scripts/smoke_cli.sh --rdc "C:/path/sample.rdc" --context cli-smoke
```

`smoke_cli.sh` directly invokes `bin/rdx` for `doctor`, tool discovery, and, when `--rdc` is passed, the capture/session chain. The release gate checks `intermediate/logs/smoke_cli.log` only when smoke reports are required.

`preview_geometry_smoke.py` validates preview window geometry and should stay aligned with CLI preview behavior.

`rdx_install.ps1` handles install, upgrade, uninstall, and doctor for self-contained Windows x64 release packages. Use `-DryRun` before mutating a real machine PATH.
