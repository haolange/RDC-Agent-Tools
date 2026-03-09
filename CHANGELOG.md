# Changelog

## Unreleased

- Added read-only `rd.vfs.*` tools and matching CLI `vfs` commands for JSON-first navigation across draws, passes, resources, pipeline, context, and artifacts.
- Fixed `scripts/release_gate.py` so `rg` fallback distinguishes literal/path scans from regex scans and no longer crashes on invalid regex assembly.
- Added `pyproject.toml` to make dependencies, pytest markers, and local development entrypoints reproducible without changing the repo-first runtime model.
- Added VFS-focused tests and release-gate regression coverage.
