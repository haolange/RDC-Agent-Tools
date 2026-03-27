"""Dedicated worker process for RenderDoc-backed runtime execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict

from rdx.io_utils import safe_json_text, safe_stream_write

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")


def _emit(payload: Dict[str, Any]) -> None:
    safe_stream_write(safe_json_text(payload) + "\n", sys.stdout)


def _context_id() -> str:
    return str(os.environ.get("RDX_CONTEXT_ID") or "default").strip() or "default"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RDX RenderDoc runtime worker")
    parser.add_argument("--context-id", default=_context_id())
    args = parser.parse_args(argv)
    context_id = str(args.context_id or "default").strip() or "default"
    os.environ["RDX_CONTEXT_ID"] = context_id

    try:
        from rdx import server
    except Exception as exc:  # noqa: BLE001
        _emit({"kind": "startup_error", "message": f"{exc.__class__.__name__}: {exc}"})
        return 1

    _emit(
        {
            "kind": "ready",
            "context_id": context_id,
            "pid": os.getpid(),
            "runtime_id": str(os.environ.get("RDX_WORKER_RUNTIME_ID") or ""),
            "cache_root": str(os.environ.get("RDX_WORKER_CACHE_ROOT") or ""),
            "source_manifest": str(os.environ.get("RDX_WORKER_SOURCE_MANIFEST") or ""),
        }
    )

    for line in sys.stdin:
        text = str(line).strip()
        if not text:
            continue
        try:
            request = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            _emit({"id": "", "ok": False, "error": {"message": f"invalid request: {exc}"}})
            continue

        req_id = str(request.get("id") or "")
        method = str(request.get("method") or "").strip()
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        try:
            if method == "exec":
                result = asyncio.run(
                    server.dispatch_operation(
                        str(params.get("operation") or ""),
                        dict(params.get("args") or {}),
                        transport=str(params.get("transport") or "daemon"),
                        remote=bool(params.get("remote", False)),
                        context_id=context_id,
                    )
                )
                _emit({"id": req_id, "ok": True, "result": result})
                continue
            if method == "clear_context":
                result = asyncio.run(
                    server.dispatch_operation(
                        "rd.core.shutdown",
                        {},
                        transport="daemon",
                        remote=False,
                        context_id=context_id,
                    )
                )
                _emit({"id": req_id, "ok": True, "result": result})
                continue
            if method == "status":
                _emit(
                    {
                        "id": req_id,
                        "ok": True,
                        "result": {
                            "running": True,
                            "pid": os.getpid(),
                            "runtime_id": str(os.environ.get("RDX_WORKER_RUNTIME_ID") or ""),
                            "cache_root": str(os.environ.get("RDX_WORKER_CACHE_ROOT") or ""),
                            "source_manifest": str(os.environ.get("RDX_WORKER_SOURCE_MANIFEST") or ""),
                        },
                    }
                )
                continue
            if method == "shutdown":
                asyncio.run(server.runtime_shutdown())
                _emit({"id": req_id, "ok": True, "result": {"stopped": True}})
                return 0
            _emit({"id": req_id, "ok": False, "error": {"message": f"unknown worker method: {method}"}})
        except Exception as exc:  # noqa: BLE001
            _emit({"id": req_id, "ok": False, "error": {"message": f"{exc.__class__.__name__}: {exc}"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
