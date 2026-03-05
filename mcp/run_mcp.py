#!/usr/bin/env python3
"""Standalone MCP launcher for rdx-tools."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path
from typing import Iterable


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


def _ensure_runtime_layout(binaries: Path, pymod: Path) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if not binaries.is_dir():
        failures.append(f"missing runtime directory: {binaries}")
    if not pymod.is_dir():
        failures.append(f"missing python module directory: {pymod}")
    if not (binaries / "renderdoc.dll").is_file():
        failures.append(f"missing renderdoc.dll: {binaries / 'renderdoc.dll'}")
    if not (pymod / "renderdoc.pyd").is_file():
        failures.append(f"missing renderdoc.pyd: {pymod / 'renderdoc.pyd'}")
    return (len(failures) == 0), failures


def _init_pythonpath() -> None:
    root = _tools_root()
    os.environ.setdefault("RDX_TOOLS_ROOT", str(root))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="rdx-tools MCP launcher")
    parser.add_argument("--ensure-env", action="store_true", help="Validate Python/runtime prerequisites and exit.")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http", "http"], default="stdio")
    parser.add_argument("--mode", choices=["lan", "internet"], default="lan")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--log-level", default=os.environ.get("RDX_LOG_LEVEL", "INFO"))
    return parser.parse_args(list(argv))


def _normalize_transport(value: str) -> str:
    return "streamable-http" if value == "http" else value


def main(argv: Iterable[str] | None = None) -> int:
    parsed = _parse_args(sys.argv[1:] if argv is None else argv)
    _init_pythonpath()

    from rdx.runtime_bootstrap import bootstrap_renderdoc_runtime
    from rdx.runtime_paths import ensure_runtime_dirs

    ensure_runtime_dirs()
    bootstrap = bootstrap_renderdoc_runtime(probe_import=bool(parsed.ensure_env))
    os.environ["RDX_LOG_LEVEL"] = str(parsed.log_level).upper()
    if parsed.host:
        os.environ["RDX_SSE_HOST"] = parsed.host
    if parsed.port:
        os.environ["RDX_SSE_PORT"] = str(parsed.port)

    missing = _check_deps()
    ok_layout, layout_errors = _ensure_runtime_layout(bootstrap.binaries_dir, bootstrap.pymodules_dir)
    if parsed.ensure_env:
        if missing:
            print("[RDX] missing python dependencies:", ", ".join(sorted(missing)))
        if layout_errors:
            for item in layout_errors:
                print(f"[RDX] {item}")
        print(f"[RDX] resolved runtime dll dir: {bootstrap.binaries_dir}")
        print(f"[RDX] resolved renderdoc module dir: {bootstrap.pymodules_dir}")
        if bootstrap.import_ok:
            if bootstrap.import_module_path:
                print(f"[RDX] renderdoc import probe ok: {bootstrap.import_module_path}")
        else:
            print(f"[RDX] renderdoc import probe failed: {bootstrap.import_error or 'unknown error'}")
            print("[RDX] hint: ensure renderdoc.dll and renderdoc.pyd are from the same build and ABI.")
            print(
                "[RDX] hint: set RDX_RUNTIME_DLL_DIR and RDX_RENDERDOC_PATH if using custom runtime location.",
            )
        return 0 if (not missing and ok_layout and bootstrap.import_ok) else 1

    if missing:
        print(f"[RDX] missing dependencies: {', '.join(sorted(missing))}", file=sys.stderr)
        return 1
    if not ok_layout:
        for item in layout_errors:
            print(f"[RDX] {item}", file=sys.stderr)
        return 1

    transport = _normalize_transport(parsed.transport)
    try:
        from rdx import server
    except Exception as exc:
        print(f"[RDX] startup failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        if transport == "sse":
            server.main_sse()
        elif transport == "streamable-http":
            server.main_streamable_http()
        else:
            server.main()
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
