"""Catalog-backed operation routing and prerequisite enforcement."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List

from rdx import server_runtime
from rdx.core.operation_registry import OperationRegistry
from rdx.handlers import (
    buffer,
    capture,
    core,
    debug,
    diag,
    event,
    export,
    macro,
    mesh,
    perf,
    pipeline,
    remote,
    replay,
    resource,
    session,
    shader,
    texture,
    util,
    vfs,
)

OperationHandler = Callable[[str, Dict[str, Any], Dict[str, Any]], Awaitable[Any]]

_DOMAIN_HANDLERS: Dict[str, Callable[[str, Dict[str, Any], Dict[str, Any]], Awaitable[Any]]] = {
    "buffer": buffer.handle,
    "capture": capture.handle,
    "core": core.handle,
    "debug": debug.handle,
    "diag": diag.handle,
    "event": event.handle,
    "export": export.handle,
    "macro": macro.handle,
    "mesh": mesh.handle,
    "perf": perf.handle,
    "pipeline": pipeline.handle,
    "remote": remote.handle,
    "replay": replay.handle,
    "resource": resource.handle,
    "session": session.handle,
    "shader": shader.handle,
    "texture": texture.handle,
    "util": util.handle,
    "vfs": vfs.handle,
}

_CATALOG_TOOLS = server_runtime._load_tool_catalog()
_TOOL_INDEX = {str(item.get("name") or "").strip(): dict(item) for item in _CATALOG_TOOLS}


def load_catalog() -> List[Dict[str, Any]]:
    return list(_CATALOG_TOOLS)


def _structured_prereq_error(tool_name: str, requirement: str, via_tools: list[str], reason: str) -> Dict[str, Any]:
    code = f"missing_prerequisite_{requirement.replace('.', '_')}"
    return {
        "success": False,
        "error_message": f"{tool_name} requires {requirement} before execution",
        "code": code,
        "category": "runtime",
        "details": {
            "prerequisite": requirement,
            "via_tools": list(via_tools),
            "reason": reason,
            "tool_name": tool_name,
        },
    }


def _structured_runtime_owner_error(tool_name: str, context_id: str, details: dict[str, Any]) -> Dict[str, Any]:
    payload = dict(details or {})
    payload.setdefault("tool_name", tool_name)
    payload.setdefault("context_id", context_id)
    return {
        "success": False,
        "error_message": f"{tool_name} conflicts with the claimed runtime owner for context {context_id}",
        "code": "runtime_owner_conflict",
        "category": "runtime",
        "details": payload,
    }


def _structured_baton_error(tool_name: str, context_id: str, details: dict[str, Any]) -> Dict[str, Any]:
    payload = dict(details or {})
    payload.setdefault("tool_name", tool_name)
    payload.setdefault("context_id", context_id)
    return {
        "success": False,
        "error_message": f"{tool_name} references an invalid runtime baton for context {context_id}",
        "code": "runtime_baton_invalid",
        "category": "validation",
        "details": payload,
    }


def _when_applies(args: Dict[str, Any], when: str) -> bool:
    if not when:
        return True
    if when == "options.remote_id_present":
        options = args.get("options")
        return isinstance(options, dict) and str(options.get("remote_id") or "").strip() != ""
    return False


def _has_prerequisite(requirement: str, args: Dict[str, Any]) -> bool:
    snapshot = server_runtime._context_snapshot()
    runtime_state = snapshot.get("runtime", {}) if isinstance(snapshot, dict) else {}
    remote_state = snapshot.get("remote", {}) if isinstance(snapshot, dict) else {}
    if requirement == "capture_file_id":
        return bool(str(args.get("capture_file_id") or runtime_state.get("capture_file_id") or "").strip())
    if requirement == "session_id":
        return bool(str(args.get("session_id") or runtime_state.get("session_id") or "").strip())
    if requirement == "remote_id":
        if str(args.get("remote_id") or "").strip():
            return True
        options = args.get("options")
        if isinstance(options, dict) and str(options.get("remote_id") or "").strip():
            return True
        return bool(str(remote_state.get("remote_id") or "").strip())
    if requirement == "active_event_id":
        event_id = int(args.get("event_id") or runtime_state.get("active_event_id") or 0)
        return event_id > 0
    if requirement == "capability.remote":
        return bool(server_runtime._runtime.enable_remote)
    return True


def _enforce_prerequisites(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any] | None:
    meta = _TOOL_INDEX.get(tool_name, {})
    for item in meta.get("prerequisites", []):
        if not isinstance(item, dict):
            continue
        requirement = str(item.get("requires") or "").strip()
        if not requirement:
            continue
        when = str(item.get("when") or "").strip()
        if not _when_applies(args, when):
            continue
        if _has_prerequisite(requirement, args):
            continue
        via_tools = [str(name).strip() for name in item.get("via_tools", []) if str(name).strip()]
        reason = str(item.get("reason") or "").strip()
        return _structured_prereq_error(tool_name, requirement, via_tools, reason)
    return None


def _tool_requires_runtime_owner(tool_name: str) -> bool:
    if tool_name.startswith(("rd.core.", "rd.util.", "rd.vfs.", "rd.diag.")):
        return False
    if tool_name.startswith("rd.session."):
        return tool_name in {"rd.session.resume"}
    return any(tool_name.startswith(prefix) for prefix in server_runtime._LIVE_OWNER_PREFIXES)


def _runtime_owner_preflight(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any] | None:
    if not _tool_requires_runtime_owner(tool_name):
        return None
    ctx = server_runtime.normalize_context_id(args.get("context_id") or server_runtime._runtime_context_id())
    state = server_runtime._context_state(ctx)
    runtime_owner = dict(state.get("runtime_owner") or {})
    active_owner = str(runtime_owner.get("agent_id") or "").strip()
    active_lease = str(runtime_owner.get("lease_id") or "").strip()
    active_status = str(runtime_owner.get("status") or "unclaimed").strip() or "unclaimed"
    if active_owner and active_status == "claimed":
        requested_owner = str(args.get("runtime_owner") or "").strip()
        requested_lease = str(args.get("owner_lease_id") or "").strip()
        if requested_owner != active_owner or requested_lease != active_lease:
            return _structured_runtime_owner_error(
                tool_name,
                ctx,
                {
                    "runtime_owner": active_owner,
                    "owner_lease_id": active_lease,
                    "requested_runtime_owner": requested_owner,
                    "requested_owner_lease_id": requested_lease,
                },
            )
    active_baton = dict(state.get("active_baton") or {})
    requested_baton = str(args.get("baton_id") or "").strip()
    active_baton_id = str(active_baton.get("baton_id") or "").strip()
    if requested_baton and active_baton_id and requested_baton != active_baton_id:
        return _structured_baton_error(
            tool_name,
            ctx,
            {
                "active_baton_id": active_baton_id,
                "requested_baton_id": requested_baton,
            },
        )
    return None


def build_operation_registry() -> OperationRegistry:
    registry = OperationRegistry()
    for tool in _CATALOG_TOOLS:
        tool_name = str(tool.get("name") or "").strip()
        parts = tool_name.split(".")
        if len(parts) != 3 or parts[0] != "rd":
            continue
        domain, action = parts[1], parts[2]
        domain_handler = _DOMAIN_HANDLERS.get(domain)
        if domain_handler is None:
            continue

        async def _handler(args: Dict[str, Any], env: Dict[str, Any], *, _tool_name: str = tool_name, _action: str = action, _domain_handler=domain_handler) -> Any:
            preflight = _enforce_prerequisites(_tool_name, args)
            if preflight is not None:
                return preflight
            owner_preflight = _runtime_owner_preflight(_tool_name, args)
            if owner_preflight is not None:
                return owner_preflight
            return await _domain_handler(_action, dict(args or {}), env)

        registry.register(tool_name, _handler)
    return registry

