#!/usr/bin/env python3
"""Standalone CLI launcher for rdx-tools."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _bootstrap_tools_root() -> Path:
    script_dir = Path(__file__).resolve().parent
    candidate = script_dir.parent
    env_root = os.environ.get("RDX_TOOLS_ROOT", "").strip()
    if env_root:
        env_path = Path(env_root).expanduser().resolve()
        if env_path.is_dir():
            if env_path != candidate:
                print(
                    f"[RDX][WARN] RDX_TOOLS_ROOT overrides launcher root ({candidate}); using {env_path}",
                    file=sys.stderr,
                )
            candidate = env_path
        else:
            print(
                f"[RDX][WARN] invalid RDX_TOOLS_ROOT='{env_path}', fallback to {candidate}",
                file=sys.stderr,
            )

    resolved = candidate.resolve()
    if str(resolved) not in sys.path:
        sys.path.insert(0, str(resolved))
    os.environ.setdefault("RDX_TOOLS_ROOT", str(resolved))
    return resolved


TOOLS_ROOT = _bootstrap_tools_root()

from rdx.io_utils import safe_json_text, safe_stream_write

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


def _normalize_context(value: str | None) -> str:
    raw = str(value or "").strip()
    return raw if raw else "default"


def _init_pythonpath() -> Path:
    from rdx.runtime_paths import ensure_tools_root_env, ensure_runtime_dirs

    root = ensure_tools_root_env()
    ensure_runtime_dirs()
    os.environ.setdefault("RDX_TOOLS_ROOT", str(root))
    os.environ.setdefault("RDX_CONTEXT_ID", _normalize_context(os.environ.get("RDX_CONTEXT_ID")))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


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


def _emit_result(payload: dict[str, object]) -> None:
    safe_stream_write(safe_json_text(payload) + "\n", sys.stdout)


def _write_out(text: str) -> None:
    safe_stream_write(text + "\n", sys.stdout)


def _write_err(text: str) -> None:
    safe_stream_write(text + "\n", sys.stderr)


def _print_launcher_help() -> None:
    for line in (
        "usage: python cli/run_cli.py <command> [--daemon-context <id>] ...",
        "commands:",
        "  daemon start|stop|status",
        "  context clear",
        "  call <operation> [--args-json ... | --args-file ...] [--format json|tsv] [--remote]",
        "  capture open|status",
        "  vfs ls|cat|tree|resolve",
        "  diff pipeline|image",
        "  assert pipeline|image",
        "",
        "examples:",
        "  python cli/run_cli.py daemon start --daemon-context local",
        "  python cli/run_cli.py context clear --daemon-context local",
        "  python cli/run_cli.py capture open --file D:\\path\\capture.rdc --frame-index 0",
        "  python cli/run_cli.py call rd.session.get_context --args-file .\\args.json --format json",
        "  python cli/run_cli.py vfs ls --path / --format tsv",
    ):
        _write_out(line)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        _write_err("[RDX] missing command, use --help for launcher usage.")
        return 2
    if "-h" in argv or "--help" in argv:
        _print_launcher_help()
        return 0

    root = _init_pythonpath()
    missing = _check_deps()
    if missing:
        _write_err(f"[RDX] missing dependencies: {', '.join(sorted(missing))}")
        _emit_result({"ok": False, "error_code": "dependencies_missing", "error_message": ", ".join(sorted(missing)), "context_id": os.environ.get("RDX_CONTEXT_ID", "default")})
        return 1

    from rdx.runtime_bootstrap import bootstrap_renderdoc_runtime
    from rdx.runtime_paths import ensure_runtime_dirs

    ensure_runtime_dirs()
    bootstrap_renderdoc_runtime(probe_import=False)

    try:
        from rdx import cli as rdx_cli
    except Exception as exc:
        _write_err(f"[RDX] startup failed: {exc.__class__.__name__}: {exc}")
        return 1

    try:
        if argv is None:
            return rdx_cli.main()
        return rdx_cli.main()
    except Exception as exc:
        _write_err(f"[RDX] startup failed: {exc.__class__.__name__}: {exc}")
        _emit_result(
            {
                "ok": False,
                "error_code": "runtime_error",
                "error_message": f"{exc}",
                "context_id": os.environ.get("RDX_CONTEXT_ID", "default"),
            }
        )
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _write_err(f"[RDX] startup failed: {exc.__class__.__name__}: {exc}")
        raise SystemExit(1)
