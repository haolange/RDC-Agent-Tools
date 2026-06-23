# Release Notes

## 1.0.0

- `rdx-tools` is a CLI-only RenderDoc `.rdc` runtime.
- The public user command is `rdx`; release packages keep `rdx.bat`, `bin/rdx`, and `python cli/run_cli.py` as launcher files.
- This is the first GA compatibility baseline. Pre-GA ownership, lease, baton, handoff, and runtime materialization experiments are intentionally outside the 1.0 public contract.
- CLI daemon contexts are isolated runtime namespaces chosen by the caller; business orchestration remains outside this package.
- Workers use the packaged runtime binaries in place instead of materializing per-run binary copies.
- `rd.remote.connect` documents both direct RenderDoc endpoints and Android adb bootstrap via `options.transport`.
- Added stable `version` and shell completion commands.
- Release packaging produces a Windows x64 self-contained zip with checksums, manifest, license inventory, and SBOM.
- Install lifecycle is handled by `scripts/rdx_install.ps1`.
- Release validation uses catalog validation, pytest, markdown health, bash CLI smoke, manifest integrity, bundled runtime checks, and package verification.

