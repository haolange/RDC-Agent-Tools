# Documentation Governance

This package is CLI-only. Documentation must not describe an `rdx-tools` MCP server, MCP transport, or built-in RDC ToolBridge MCP descriptor.

Keep navigation links current and follow [../AGENTS.md](../AGENTS.md). User-facing docs should mention CLI commands, not Python bootstrap internals, except in maintainer sections.

User-facing installation and quickstart docs must not tell users to run package managers, create virtual environments, or restore dependencies from a lock file. GA artifacts are self-contained; dependency provenance is tracked through `pyproject.toml`, the bundled runtime manifest, license inventory, and SBOM.

`spec/doc_extracted.txt` is the repo-local source used to generate `spec/tool_catalog.json`. Treat it as maintainer catalog input, not user documentation.

Changes that touch `rd.session.open_preview` or preview geometry must keep `preview_geometry_smoke.py` and the user-facing preview documentation synchronized.