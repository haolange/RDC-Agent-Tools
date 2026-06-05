"""RDX runtime dispatch helpers for the daemon-backed CLI."""

from __future__ import annotations

from typing import Any, Dict, Optional

from rdx.core.artifact_publisher import ArtifactPublisher
from rdx.core.contracts import canonical_error
from rdx.core.errors import map_exception
from rdx.core.engine import CoreEngine, ExecutionContext
from rdx.progress import ProgressReporter, ProgressSink
 
_core_engine: Optional[CoreEngine] = None
_PREVIEW_SELF_SYNCED_OPERATIONS = {
    "rd.capture.open_replay",
    "rd.event.set_active",
    "rd.replay.set_frame",
    "rd.session.get_context",
    "rd.session.select_session",
    "rd.session.resume",
}


def _runtime_module() -> Any:
    from rdx import server_runtime

    return server_runtime


def __getattr__(name: str) -> Any:
    if name == "server_runtime":
        return _runtime_module()
    return getattr(_runtime_module(), name)


def _get_core_engine() -> CoreEngine:
    global _core_engine
    if _core_engine is None:
        from rdx.tool_router import build_operation_registry

        _core_engine = CoreEngine(
            registry=build_operation_registry(),
            artifact_publisher=ArtifactPublisher(),
        )
    return _core_engine


def get_core_engine() -> CoreEngine:
    return _get_core_engine()


async def runtime_startup() -> None:
    await _runtime_module().runtime_startup()


async def runtime_shutdown() -> None:
    await _runtime_module().runtime_shutdown()


async def dispatch_operation(
    operation: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    transport: str = "core",
    remote: bool = False,
    context_id: Optional[str] = None,
    progress_sink: Optional[ProgressSink] = None,
) -> Dict[str, Any]:
    server_runtime = _runtime_module()
    await server_runtime.runtime_startup()
    call_args = dict(args or {})
    chosen_context_id = server_runtime.normalize_context_id(
        context_id
        or call_args.get("context_id")
        or server_runtime._runtime_context_id()
    )
    ctx = ExecutionContext(
        transport=transport,
        remote=remote,
        metadata={"context_id": chosen_context_id},
    )
    ctx.progress_reporter = ProgressReporter(
        trace_id=ctx.trace_id,
        operation=operation,
        sink=progress_sink,
    )
    arg_keys = ",".join(sorted(call_args.keys())) if call_args else "-"
    server_runtime.logger.info(
        "op.start transport=%s remote=%s op=%s trace_id=%s arg_keys=%s",
        transport,
        remote,
        operation,
        ctx.trace_id,
        arg_keys,
    )
    context_token = server_runtime._CURRENT_CONTEXT_ID.set(chosen_context_id)
    progress_token = server_runtime._CURRENT_PROGRESS_REPORTER.set(ctx.progress_reporter)
    try:
        server_runtime._record_operation_start(
            chosen_context_id,
            trace_id=ctx.trace_id,
            operation=operation,
            transport=transport,
            args=call_args,
        )
        try:
            await server_runtime.ensure_context_ready(chosen_context_id)
            if operation not in {"rd.session.open_preview", "rd.session.close_preview"} | _PREVIEW_SELF_SYNCED_OPERATIONS:
                await server_runtime._auto_sync_preview_if_enabled(chosen_context_id)
            payload = await _get_core_engine().execute(operation, call_args, context=ctx)
            if isinstance(payload, dict):
                server_runtime._postprocess_context_snapshot(operation, call_args, payload, ctx)
        except Exception as exc:  # noqa: BLE001
            core_exc = map_exception(exc)
            payload = canonical_error(
                result_kind=str(operation),
                code=core_exc.code,
                category=core_exc.category,
                message=core_exc.message,
                details=core_exc.details,
                trace_id=ctx.trace_id,
                transport=ctx.transport,
            )
        if operation not in {"rd.session.open_preview", "rd.session.close_preview"} | _PREVIEW_SELF_SYNCED_OPERATIONS:
            await server_runtime._auto_sync_preview_if_enabled(chosen_context_id)
        server_runtime._record_operation_finish(
            chosen_context_id,
            trace_id=ctx.trace_id,
            payload=payload,
        )
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        server_runtime.logger.info(
            "op.done transport=%s op=%s trace_id=%s ok=%s duration_ms=%s",
            transport,
            operation,
            str(meta.get("trace_id") or ctx.trace_id),
            bool(payload.get("ok")) if isinstance(payload, dict) else False,
            meta.get("duration_ms"),
        )
        return payload
    finally:
        server_runtime._CURRENT_PROGRESS_REPORTER.reset(progress_token)
        server_runtime._CURRENT_CONTEXT_ID.reset(context_token)


def main() -> None:
    raise RuntimeError("rdx-tools no longer exposes an MCP server; use `rdx.bat --json doctor`, `bin/rdx --json doctor`, or `rdx.bat call <rd.*>`.")
