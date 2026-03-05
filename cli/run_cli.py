#!/usr/bin/env python3
"""Standalone CLI launcher for rdx-tools."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REQUIRED_DEPENDENCIES: list[tuple[str, str]] = [
    ("mcp", "mcp.server.fastmcp"),
    ("mcp", "mcp.server.transport_security"),
    ("pydantic", "pydantic"),
    ("numpy", "numpy"),
    ("Pillow", "PIL"),
    ("pyarrow", "pyarrow"),
    ("jinja2", "jinja2"),
    ("aiofiles", "aiofiles"),
]


def _tools_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _init_pythonpath() -> None:
    root = _tools_root()
    os.environ.setdefault("RDX_TOOLS_ROOT", str(root))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _module_available(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except ModuleNotFoundError:
        return False


def _check_deps() -> list[str]:
    missing: list[str] = []
    for dist_name, import_name in REQUIRED_DEPENDENCIES:
        if (not _module_available(import_name)) and dist_name not in missing:
            missing.append(dist_name)
    return missing


def main() -> int:
    _init_pythonpath()
    missing = _check_deps()
    if missing:
        print(f"[RDX] missing dependencies: {', '.join(sorted(missing))}", file=sys.stderr)
        return 1

    from rdx.runtime_bootstrap import bootstrap_renderdoc_runtime
    from rdx.runtime_paths import ensure_runtime_dirs

    ensure_runtime_dirs()
    bootstrap_renderdoc_runtime(probe_import=False)

    try:
        from rdx import cli as rdx_cli
    except Exception as exc:
        print(f"[RDX] startup failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        rdx_cli.main()
    except Exception as exc:
        print(f"[RDX] startup failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[RDX] startup failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
