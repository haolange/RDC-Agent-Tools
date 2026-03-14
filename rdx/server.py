"""RDX MCP server entrypoint backed by the catalog router and namespace handlers."""

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

from rdx import server_runtime
from rdx.core.artifact_publisher import ArtifactPublisher
from rdx.core.contracts import canonical_error, env_bool
from rdx.core.engine import CoreEngine, ExecutionContext
from rdx.daemon.client import daemon_request
from rdx.progress import ProgressReporter, ProgressSink
from rdx.timeout_policy import daemon_exec_timeout_s
from rdx.tool_router import build_operation_registry, load_catalog

CaptureFileHandle = server_runtime.CaptureFileHandle
ReplayHandle = server_runtime.ReplayHandle
RemoteHandle = server_runtime.RemoteHandle
ConsumedRemoteHandle = server_runtime.ConsumedRemoteHandle
ShaderDebugHandle = server_runtime.ShaderDebugHandle
RuntimeState = server_runtime.RuntimeState

_runtime = server_runtime._runtime
_CATALOG_TOOLS = load_catalog()
_operation_registry = build_operation_registry()
_core_engine = CoreEngine(
    registry=_operation_registry,
    artifact_publisher=ArtifactPublisher(),
)


def __getattr__(name: str) -> Any:
    return getattr(server_runtime, name)


def _mcp_uses_daemon() -> bool:
    return env_bool("RDX_MCP_USE_DAEMON", False)


def _mcp_daemon_context() -> str:
    return server_runtime._runtime_context_id()


@asynccontextmanager
async def _lifespan(_: FastMCP):
    if _mcp_uses_daemon():
        yield
        return
    await server_runtime.runtime_startup()
    try:
        yield
    finally:
        await server_runtime.runtime_shutdown()


def get_core_engine() -> CoreEngine:
    return _core_engine


async def dispatch_operation(
    operation: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    transport: str = "core",
    remote: bool = False,
    context_id: Optional[str] = None,
    progress_sink: Optional[ProgressSink] = None,
) -> Dict[str, Any]:
    await server_runtime.runtime_startup()
    call_args = dict(args or {})
    chosen_context_id = server_runtime.normalize_context_id(context_id or server_runtime._runtime_context_id())
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
        payload = await _core_engine.execute(operation, call_args, context=ctx)
        if isinstance(payload, dict):
            server_runtime._postprocess_context_snapshot(operation, call_args, payload, ctx)
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
    if _mcp_uses_daemon():
        daemon_args = dict(args or {})
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
                context=_mcp_daemon_context(),
            )
            payload = response.get("result")
            if isinstance(payload, dict):
                return json.dumps(payload, ensure_ascii=False, default=server_runtime._json_default)
            raise RuntimeError("daemon returned invalid MCP payload")
        except Exception as exc:  # noqa: BLE001
            payload = canonical_error(
                result_kind=tool_name,
                code="runtime_error",
                category="runtime",
                message=str(exc),
                transport="mcp",
            )
            return json.dumps(payload, ensure_ascii=False, default=server_runtime._json_default)

    payload = await dispatch_operation(tool_name, args, transport="mcp", remote=tool_name.startswith("rd.remote."))
    return json.dumps(payload, ensure_ascii=False, default=server_runtime._json_default)


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
