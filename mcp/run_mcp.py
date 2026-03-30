#!/usr/bin/env python3
"""Standalone MCP launcher for rdx-tools."""

from __future__ import annotations

import atexit
import argparse
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Iterable, Any


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
from rdx.python_runtime import current_python_runtime_details, validate_bundled_python_layout
from rdx.runtime_requirements import missing_dependencies

RETURN_OK = 0
RETURN_ARGS_ERROR = 1
RETURN_ENV_ERROR = 2
RETURN_STARTUP_ERROR = 3
RETURN_TIMEOUT = 4
RETURN_TOOL_ERROR = 5
HEARTBEAT_INTERVAL_S = 60.0


def _normalize_context(value: str | None) -> str:
    raw = str(value or "").strip()
    return raw if raw else "default"


def _launcher_prog(default: str) -> str:
    return str(os.environ.get("RDX_LAUNCHER_PROG") or default).strip() or default


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=_launcher_prog("python mcp/run_mcp.py"),
        description="rdx-tools MCP launcher",
    )
    parser.add_argument("--ensure-env", action="store_true", help="Validate Python/runtime prerequisites and exit.")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http", "http"], default="stdio")
    parser.add_argument("--mode", choices=["lan", "internet"], default="lan")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--log-level", default=os.environ.get("RDX_LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--daemon-context",
        default="default",
        help="Context id for daemon lifecycle and state isolation.",
    )
    parser.add_argument(
        "--context-id",
        default="",
        dest="context_id",
        help="Compatibility alias for daemon context.",
    )
    return parser.parse_args(list(argv))


def _check_deps() -> list[str]:
    return missing_dependencies()


def _emit_payload(payload: dict[str, Any]) -> None:
    safe_stream_write(safe_json_text(payload) + "\n", sys.stdout)


def _write_err(text: str) -> None:
    safe_stream_write(text + "\n", sys.stderr)


def _normalize_transport(value: str) -> str:
    return "streamable-http" if value == "http" else value


def _check_renderdoc_runtime_layout(binaries: Path, pymod: Path) -> tuple[bool, list[str]]:
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


def _emit_runtime_env_error(
    code: str,
    message: str,
    *,
    context_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    _emit_payload(
        {
            "ok": False,
            "error_code": code,
            "error_message": message,
            "context_id": context_id,
            "details": details or {},
        }
    )


def _python_env_diagnostics() -> tuple[bool, list[str], dict[str, Any]]:
    ok, failures, details = validate_bundled_python_layout()
    payload = dict(details)
    payload.setdefault("current_python", current_python_runtime_details())
    if failures:
        payload["failures"] = list(failures)
    return ok, failures, payload


def main(argv: Iterable[str] | None = None) -> int:
    parsed = _parse_args(sys.argv[1:] if argv is None else argv)

    transport = _normalize_transport(parsed.transport)
    context_id = _normalize_context(parsed.context_id or parsed.daemon_context)

    from rdx.daemon.client import (
        attach_client,
        cleanup_stale_daemon_states,
        detach_client,
        ensure_daemon,
        heartbeat_client,
    )
    from rdx.runtime_materializer import load_runtime_source
    from rdx.runtime_paths import ensure_runtime_dirs, ensure_tools_root_env

    try:
        root = ensure_tools_root_env()
    except Exception as exc:  # noqa: BLE001
        _emit_runtime_env_error("runtime_root_invalid", f"{exc}", context_id=context_id)
        return RETURN_ENV_ERROR

    os.environ.setdefault("RDX_CONTEXT_ID", context_id)
    os.environ.setdefault("RDX_LOG_LEVEL", str(parsed.log_level).upper())
    if parsed.host:
        os.environ["RDX_SSE_HOST"] = str(parsed.host)
    if parsed.port:
        os.environ["RDX_SSE_PORT"] = str(parsed.port)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    ensure_runtime_dirs()

    python_ok, python_errors, python_details = _python_env_diagnostics()
    if not python_ok:
        _emit_runtime_env_error(
            "python_runtime_incomplete",
            "; ".join(python_errors),
            context_id=context_id,
            details=python_details,
        )
        return RETURN_ENV_ERROR

    try:
        runtime_source = load_runtime_source()
    except Exception as exc:  # noqa: BLE001
        _emit_runtime_env_error(
            "runtime_layout_missing",
            str(exc),
            context_id=context_id,
            details={"python": python_details},
        )
        return RETURN_ENV_ERROR

    missing = _check_deps()
    ok_layout, layout_errors = _check_renderdoc_runtime_layout(runtime_source.binaries_dir, runtime_source.pymodules_dir)

    if parsed.ensure_env:
        if missing:
            _emit_runtime_env_error(
                "dependencies_missing",
                "missing python dependencies: " + ", ".join(sorted(missing)),
                context_id=context_id,
                details={
                    "python": python_details,
                    "missing_dependencies": sorted(missing),
                },
            )
            return RETURN_ENV_ERROR

        if not ok_layout:
            _emit_runtime_env_error(
                "runtime_layout_missing",
                "; ".join(layout_errors),
                context_id=context_id,
                details={
                    "python": python_details,
                    "renderdoc_failures": layout_errors,
                },
            )
            return RETURN_ENV_ERROR

        _emit_payload(
            {
                "ok": True,
                "error_code": 0,
                "error_message": "",
                "context_id": context_id,
                "details": {
                    "layout_ok": True,
                    "tools_root": str(root),
                    "python": python_details,
                    "renderdoc": {
                        "source_manifest": str(runtime_source.manifest_path),
                        "renderdoc_dll": str(runtime_source.binaries_dir / "renderdoc.dll"),
                        "renderdoc_pyd": str(runtime_source.pymodules_dir / "renderdoc.pyd"),
                    },
                },
            }
        )
        return RETURN_OK

    if missing:
        _write_err(f"[RDX] missing dependencies: {', '.join(sorted(missing))}")
        _emit_runtime_env_error(
            "dependencies_missing",
            "missing python dependencies",
            context_id=context_id,
            details={"python": python_details, "missing_dependencies": sorted(missing)},
        )
        return RETURN_ARGS_ERROR

    if not ok_layout:
        for item in layout_errors:
            _write_err(f"[RDX] {item}")
        _emit_runtime_env_error(
            "runtime_layout_missing",
            "; ".join(layout_errors),
            context_id=context_id,
            details={"python": python_details, "renderdoc_failures": layout_errors},
        )
        return RETURN_ENV_ERROR

    cleanup_stale_daemon_states(context=context_id)
    ok_daemon, daemon_message, _ = ensure_daemon(context=context_id)
    if not ok_daemon:
        _emit_runtime_env_error("startup_failed", daemon_message, context_id=context_id)
        return RETURN_STARTUP_ERROR

    client_id = f"mcp-{uuid.uuid4().hex[:10]}"
    ok_attach, attach_message, _ = attach_client(
        context=context_id,
        client_id=client_id,
        client_type="mcp",
        pid=os.getpid(),
    )
    if not ok_attach:
        _emit_runtime_env_error("startup_failed", attach_message, context_id=context_id)
        return RETURN_STARTUP_ERROR

    stop_event = threading.Event()

    def _detach_client() -> None:
        stop_event.set()
        try:
            detach_client(context=context_id, client_id=client_id)
        except Exception:
            pass

    def _heartbeat_loop() -> None:
        while not stop_event.wait(HEARTBEAT_INTERVAL_S):
            try:
                heartbeat_client(context=context_id, client_id=client_id, pid=os.getpid())
            except Exception:
                break

    atexit.register(_detach_client)
    heartbeat_thread = threading.Thread(target=_heartbeat_loop, name="rdx-mcp-heartbeat", daemon=True)
    heartbeat_thread.start()

    try:
        from rdx import server
    except Exception as exc:  # noqa: BLE001
        _write_err(f"[RDX] startup failed: {exc.__class__.__name__}: {exc}")
        _emit_runtime_env_error("startup_failed", f"{exc}", context_id=context_id)
        return RETURN_STARTUP_ERROR

    try:
        if transport == "sse":
            server.main_sse()
        elif transport == "streamable-http":
            server.main_streamable_http()
        else:
            server.main()
    except Exception as exc:  # noqa: BLE001
        _write_err(f"[RDX] startup failed: {exc.__class__.__name__}: {exc}")
        _emit_runtime_env_error("startup_failed", f"{exc}", context_id=context_id)
        return RETURN_STARTUP_ERROR
    finally:
        _detach_client()
    return RETURN_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _write_err(f"[RDX] startup failed: {exc.__class__.__name__}: {exc}")
        raise SystemExit(RETURN_TOOL_ERROR)