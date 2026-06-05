# Release Notes

## 1.0.0

- `rdx-tools` is a CLI-only RenderDoc `.rdc` runtime.
- Public entrypoints are `rdx.bat`, `bin/rdx`, and `python cli/run_cli.py`.
- Added stable `version` and shell completion commands.
- Release packaging produces a Windows x64 self-contained zip with checksums, manifest, license inventory, and SBOM.
- Install lifecycle is handled by `scripts/rdx_install.ps1`.
- Release validation uses catalog validation, pytest, markdown health, bash CLI smoke, manifest integrity, bundled runtime checks, and package verification.

