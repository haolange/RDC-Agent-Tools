# Public Contract

The public user surface is the `rdx` CLI command. Release packages also include platform launcher files such as `rdx.bat`, `bin/rdx`, and `python cli/run_cli.py` for installation diagnostics and maintainer use.

`rdx-tools` 1.x keeps the canonical JSON envelope stable. Use `rdx version --json` to inspect the tool version, schema version, platform, entrypoints, and public contract metadata.

Version 1.0.0 is the first GA release. Pre-GA ownership, lease, baton, handoff, and runtime materialization experiments are not part of the 1.0 public contract. The 1.0 contract starts at the CLI entrypoints, the canonical JSON envelope, documented context commands, facade commands, and the 194-tool `rd.*` catalog shipped in the release package.

`--daemon-context <id>` selects an isolated CLI runtime namespace. It is not an ownership claim or task handoff mechanism; callers choose and manage context ids in their own orchestration layer.
