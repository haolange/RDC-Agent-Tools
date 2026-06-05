# Compatibility Notes

`rdx-tools` no longer supports MCP entrypoints. The compatible public surface is CLI:

- `rdx.bat`
- `bin/rdx`
- `python cli/run_cli.py`

The desktop RDC-Agent may still keep generic MCP client settings for other integrations. This package does not ship a built-in RDC ToolBridge MCP descriptor.

`rdx-tools` 1.x keeps the canonical JSON envelope stable. Use `rdx.bat version --json` to inspect the tool version, schema version, platform, and compatibility metadata.
