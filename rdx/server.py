"""RDX server helpers and daemon-backed MCP entrypoint."""

from __future__ import annotations

import inspect
import json
import logging
import os
import textwrap
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Sequence

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from rdx.core.artifact_publisher import ArtifactPublisher
from rdx.core.contracts import canonical_error
from rdx.core.errors import map_exception
from rdx.core.engine import CoreEngine, ExecutionContext
from rdx.daemon.client import daemon_request
from rdx.io_utils import safe_json_text
from rdx.progress import ProgressReporter, ProgressSink
from rdx.runtime_catalog import load_tool_catalog
from rdx.timeout_policy import daemon_exec_timeout_s
 
_CATALOG_TOOLS = load_tool_catalog()
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


def _mcp_daemon_context() -> str:
    return str(os.environ.get("RDX_CONTEXT_ID") or "default").strip() or "default"


@asynccontextmanager
async def _lifespan(_: FastMCP):
    yield


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


async def _dispatch_tool(tool_name: str, args: Dict[str, Any]) -> str:
    daemon_args = dict(args or {})
    chosen_context = _runtime_module().normalize_context_id(daemon_args.get("context_id") or _mcp_daemon_context())
    try:
        response = daemon_request(
            "exec",
            params={
                "operation": tool_name,
                "args": daemon_args,
                "transport": "mcp",
                "remote": tool_name.startswith("rd.remote."),
            },
            timeout=daemon_exec_timeout_s(tool_name, daemon_args),
            context=chosen_context,
        )
        payload = response.get("result")
        if isinstance(payload, dict):
            return safe_json_text(payload)
        raise RuntimeError("daemon returned invalid MCP payload")
    except Exception as exc:  # noqa: BLE001
        payload = canonical_error(
            result_kind=tool_name,
            code=str(getattr(exc, "code", "") or "runtime_error"),
            category=str(getattr(exc, "category", "") or "runtime"),
            message=str(exc),
            details=getattr(exc, "details", {}) if isinstance(getattr(exc, "details", {}), dict) else {},
            transport="mcp",
        )
        return safe_json_text(payload)


def _build_tool_callable(tool_name: str, param_names: Sequence[str]) -> Any:
    unique = []
    for name in param_names:
        if isinstance(name, str) and name.isidentifier() and name not in unique:
            unique.append(name)
    signature = ", ".join(f"{name}: Any = None" for name in unique)
    if signature:
        src = textwrap.dedent(
            f"""
            async def _tool({signature}):
                params = locals().copy()
                return await _dispatch_tool({tool_name!r}, params)
            """,
        )
    else:
        src = textwrap.dedent(
            f"""
            async def _tool():
                return await _dispatch_tool({tool_name!r}, {{}})
            """,
        )
    namespace: Dict[str, Any] = {"Any": Any, "_dispatch_tool": _dispatch_tool}
    exec(src, namespace)
    fn = namespace["_tool"]
    fn.__name__ = tool_name.replace(".", "_")
    return fn


def _create_mcp() -> FastMCP:
    kwargs: Dict[str, Any] = {}
    description = f"RenderDoc MCP tools ({len(_CATALOG_TOOLS)} doc tools)"
    try:
        params = set(inspect.signature(FastMCP.__init__).parameters)
        if "description" in params:
            kwargs["description"] = description
        if "lifespan" in params:
            kwargs["lifespan"] = _lifespan
        if "host" in params:
            kwargs["host"] = os.environ.get("RDX_SSE_HOST", "127.0.0.1")
        if "port" in params:
            kwargs["port"] = int(os.environ.get("RDX_SSE_PORT", "8765"))
        if "transport_security" in params:
            hosts = [h.strip() for h in os.environ.get("RDX_ALLOWED_HOSTS", "").split(",") if h.strip()]
            origins = [o.strip() for o in os.environ.get("RDX_ALLOWED_ORIGINS", "").split(",") if o.strip()]
            if hosts or origins:
                kwargs["transport_security"] = TransportSecuritySettings(
                    enable_dns_rebinding_protection=True,
                    allowed_hosts=hosts,
                    allowed_origins=origins,
                )
    except Exception:
        kwargs["lifespan"] = _lifespan
    return FastMCP("rdx-mcp", **kwargs)


mcp = _create_mcp()
for tool in _CATALOG_TOOLS:
    name = str(tool["name"])
    params = list(tool.get("param_names", []))
    fn = _build_tool_callable(name, params)
    fn.__doc__ = str(tool.get("description", ""))
    mcp.tool(name=name)(fn)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("RDX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    mcp.run(transport="stdio")


def main_sse() -> None:
    logging.basicConfig(
        level=os.environ.get("RDX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    mcp.run(transport="sse")


def main_streamable_http() -> None:
    logging.basicConfig(
        level=os.environ.get("RDX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    mcp.run(transport="streamable-http")
