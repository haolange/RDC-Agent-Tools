# Changelog

## Unreleased

- Added persistent context/session state with local `.rdc` warm resume, multi-session `current_session_id` selection, trace-linked operation history, runtime metrics, and bounded event-tree pagination.
- Added `rd.session.list_sessions`, `rd.session.select_session`, `rd.session.resume`, `rd.core.get_operation_history`, `rd.core.get_runtime_metrics`, `rd.core.list_tools`, `rd.core.search_tools`, and `rd.core.get_tool_graph`.
- Added read-only `rd.vfs.*` tools and matching CLI `vfs` commands for JSON-first navigation across draws, passes, resources, pipeline, context, and artifacts.
- Fixed `scripts/release_gate.py` so `rg` fallback distinguishes literal/path scans from regex scans and no longer crashes on invalid regex assembly.
- Added `pyproject.toml` to make dependencies, pytest markers, and local development entrypoints reproducible without changing the repo-first runtime model.
- Added VFS-focused tests and release-gate regression coverage.
