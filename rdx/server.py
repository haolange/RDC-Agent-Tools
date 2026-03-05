"""
RDX-MCP server with registry-driven tool registration.

- Registers all 196 doc-defined tools from `rdx/spec/tool_catalog_196.json`
- Normalizes all tool responses to:
  - success: bool
  - error_message?: str
"""

from __future__ import annotations

import asyncio
import csv
import difflib
import hashlib
import inspect
import io
import json
import logging
import os
import re
import shutil
import struct
import sys
import textwrap
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from rdx.config import RdxConfig
from rdx.core.artifact_publisher import ArtifactPublisher
from rdx.core.engine import CoreEngine, ExecutionContext
from rdx.core.event_graph import EventGraphService
from rdx.core.operation_registry import OperationRegistry
from rdx.core.perf_service import PerfService
from rdx.core.pipeline_service import PipelineService
from rdx.core.render_service import RenderService
from rdx.core.session_manager import SessionError, SessionManager
from rdx.models import _new_id
from rdx.runtime_bootstrap import bootstrap_renderdoc_runtime
from rdx.runtime_paths import artifacts_dir, ensure_runtime_dirs, runtime_root
from rdx.utils.artifact_store import ArtifactStore

logger = logging.getLogger("rdx.server")


@dataclass
class CaptureFileHandle:
    capture_file_id: str
    file_path: str
    read_only: bool
    driver: str = ""
    opened_at_ms: int = field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp() * 1000))


@dataclass
class ReplayHandle:
    session_id: str
    capture_file_id: str
    frame_index: int = 0
    active_event_id: int = 0


@dataclass
class RemoteHandle:
    remote_id: str
    host: str
    port: int
    connected: bool
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ShaderDebugHandle:
    shader_debug_id: str
    session_id: str
    mode: str
    event_id: int
    trace: Any
    debugger: Any
    current_state: Any = None
    breakpoints: List[Dict[str, Any]] = field(default_factory=list)
    stopped_reason: str = "running"


@dataclass
class RuntimeState:
    config: Dict[str, Any] = field(default_factory=dict)
    logs: List[Dict[str, Any]] = field(default_factory=list)
    captures: Dict[str, CaptureFileHandle] = field(default_factory=dict)
    replays: Dict[str, ReplayHandle] = field(default_factory=dict)
    aliases: Dict[str, str] = field(default_factory=dict)
    remotes: Dict[str, RemoteHandle] = field(default_factory=dict)
    app_capture_options: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    shader_debugs: Dict[str, ShaderDebugHandle] = field(default_factory=dict)
    shader_replacements: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    initialized: bool = False
    enable_remote: bool = True
    enable_app_api: bool = False


_config: Optional[RdxConfig] = None
_session_manager: Optional[SessionManager] = None
_event_graph_service: Optional[EventGraphService] = None
_render_service: Optional[RenderService] = None
_pipeline_service: Optional[PipelineService] = None
_perf_service: Optional[PerfService] = None
_artifact_store: Optional[ArtifactStore] = None
_runtime: RuntimeState = RuntimeState()
_runtime_bootstrapped: bool = False
_operation_registry: Optional[OperationRegistry] = None
_core_engine: Optional[CoreEngine] = None


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _record_log(level: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
    _runtime.logs.append(
        {
            "ts_ms": _now_ms(),
            "level": level.lower(),
            "message": message,
            "context": context or {},
        },
    )
    if len(_runtime.logs) > 5000:
        _runtime.logs = _runtime.logs[-5000:]


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def _ok(**fields: Any) -> str:
    payload: Dict[str, Any] = {"success": True}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _err(message: str, **fields: Any) -> str:
    payload: Dict[str, Any] = {"success": False, "error_message": str(message)}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _parse_json_like(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return value
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _as_dict(value: Any, *, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    parsed = _parse_json_like(value)
    if parsed is None:
        return default or {}
    if isinstance(parsed, dict):
        return parsed
    raise ValueError(f"Expected dict-compatible value, got: {type(parsed).__name__}")


def _as_list(value: Any, *, default: Optional[List[Any]] = None) -> List[Any]:
    parsed = _parse_json_like(value)
    if parsed is None:
        return default or []
    if isinstance(parsed, list):
        return parsed
    raise ValueError(f"Expected list-compatible value, got: {type(parsed).__name__}")


def _parse_query_like(value: Any) -> Dict[str, Any]:
    parsed = _parse_json_like(value)
    if parsed is None:
        return {}
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, str):
        query = parsed.strip()
        if not query:
            return {}
        return {"name_contains": query}
    raise ValueError(f"Expected query as dict/str, got: {type(parsed).__name__}")


def _parse_target_like(value: Any) -> Dict[str, Any]:
    parsed = _parse_json_like(value)
    if parsed is None:
        return {}
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, str):
        target_id = parsed.strip()
        if not target_id:
            return {}
        return {"texture_id": target_id}
    if isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
        return {"texture_id": str(parsed)}
    raise ValueError(f"Expected target as dict/str/int, got: {type(parsed).__name__}")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    return int(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


async def _offload(fn: Any, *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _get_rd() -> Any:
    bootstrap_renderdoc_runtime(probe_import=False)
    import renderdoc as rd
    return rd


def _check_status(status: Any, operation: str) -> None:
    rd = _get_rd()
    if status != rd.ResultCode.Succeeded:
        raise RuntimeError(f"{operation} failed with status: {status}")


def _require(fields: Dict[str, Any], *names: str) -> None:
    missing = []
    for name in names:
        value = fields.get(name)
        if value is None:
            missing.append(name)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(name)
    if missing:
        raise ValueError(f"Missing required parameter(s): {', '.join(missing)}")


def _tool_catalog_path() -> Path:
    from rdx.runtime_paths import tools_root

    return tools_root() / "spec" / "tool_catalog_196.json"


def _load_tool_catalog() -> List[Dict[str, Any]]:
    path = _tool_catalog_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    tools = list(data.get("tools", []))
    if len(tools) != 196:
        raise RuntimeError(f"Catalog must contain 196 tools, got {len(tools)}")
    names = [str(t.get("name", "")).strip() for t in tools]
    if len(set(names)) != 196:
        raise RuntimeError("Catalog contains duplicate tool names")
    return tools


def _resource_keys(resource_id: Any) -> List[str]:
    keys = [str(resource_id)]
    try:
        keys.append(str(int(resource_id)))
    except Exception:
        pass
    return keys


_FILE_SUFFIX_MAP: Dict[str, str] = {
    "png": ".png",
    "jpg": ".jpg",
    "jpeg": ".jpg",
    "dds": ".dds",
    "exr": ".exr",
    "hdr": ".hdr",
    "tga": ".tga",
    "bmp": ".bmp",
    "raw": ".raw",
}

_DEPTH_HINTS = ("depth", "stencil", "d16", "d24", "d32", "dsv", "s8")
_HDR_HINTS = ("16f", "32f", "float", "r11g11b10", "rgb10a2", "bc6")
_COMPRESSED_HINTS = ("bc1", "bc2", "bc3", "bc4", "bc5", "bc6", "bc7", "etc", "astc", "pvrtc", "atc")
_NORMAL_HINTS = ("normal", "nrm", "norm")
_MASK_HINTS = ("rough", "metal", "ao", "orm", "mask", "spec", "gloss", "height")
_COLOR_HINTS = ("albedo", "basecolor", "base_color", "diffuse", "color")


def _resource_id_tokens(resource_id: Any) -> List[str]:
    text = str(resource_id).strip()
    if not text:
        return []
    tokens = [text]
    try:
        tokens.append(str(int(text)))
    except Exception:
        pass
    return list(dict.fromkeys(tokens))


def _resource_id_matches(left: Any, right: Any) -> bool:
    lhs = set(_resource_id_tokens(left))
    rhs = set(_resource_id_tokens(right))
    return bool(lhs and rhs and lhs.intersection(rhs))


def _safe_name_token(value: str, fallback: str = "unnamed") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", "_", text).strip(" ._")
    if not text:
        return fallback
    if len(text) > 120:
        text = text[:120].rstrip("._")
    return text or fallback


def _compose_texture_name_info(
    resource_id: Any,
    *,
    resource_name: str = "",
    binding_names: Optional[Sequence[str]] = None,
    alias_name: str = "",
) -> Dict[str, Any]:
    rid = str(resource_id)
    src_name = str(resource_name or "").strip()
    alias = str(alias_name or "").strip()
    clean_binding_names: List[str] = []
    for item in binding_names or []:
        name = str(item or "").strip()
        if not name:
            continue
        if name not in clean_binding_names:
            clean_binding_names.append(name)
    primary_binding = clean_binding_names[0] if clean_binding_names else ""

    base_name = alias or src_name
    if not base_name and primary_binding:
        display_name = primary_binding
    elif base_name and primary_binding and base_name.lower() != primary_binding.lower():
        display_name = f"{base_name}@{primary_binding}"
    elif base_name:
        display_name = base_name
    else:
        digit_chunks = re.findall(r"\d+", rid)
        if digit_chunks:
            rid_token = digit_chunks[-1]
        else:
            rid_token = re.sub(r"\W+", "", rid)[-8:] or "id"
        display_name = f"tex_{rid_token}"

    return {
        "resource_id": rid,
        "resource_name": src_name,
        "alias_name": alias,
        "binding_names": clean_binding_names,
        "display_name": display_name,
        "name_stem": _safe_name_token(display_name),
    }


def _normalize_export_format(value: Any) -> str:
    fmt = str(value or "png").strip().lower()
    if fmt == "jpeg":
        return "jpg"
    return fmt


def _parse_requested_formats(value: Any) -> List[str]:
    parsed = _parse_json_like(value)
    if parsed is None:
        return ["png"]
    if isinstance(parsed, list):
        tokens = [str(item).strip() for item in parsed if str(item).strip()]
    else:
        text = str(parsed).strip()
        if not text:
            tokens = ["png"]
        else:
            tokens = [token for token in re.split(r"[,\s|;/]+", text) if token]
    normalized: List[str] = []
    for token in tokens:
        fmt = _normalize_export_format(token)
        if fmt and fmt not in normalized:
            normalized.append(fmt)
    return normalized or ["png"]


def _texture_format_name(texture_desc: Optional[Any]) -> str:
    if texture_desc is None:
        return ""
    fmt = getattr(texture_desc, "format", None)
    if fmt is None:
        return ""
    try:
        name_fn = getattr(fmt, "Name", None)
        if callable(name_fn):
            return str(name_fn())
    except Exception:
        pass
    return str(fmt)


def _recommend_formats_for_texture(
    texture_desc: Optional[Any],
    *,
    name_info: Optional[Dict[str, Any]] = None,
    for_screenshot: bool = False,
) -> List[str]:
    format_name = _texture_format_name(texture_desc).lower()
    names_blob = " ".join(
        [
            str((name_info or {}).get("resource_name", "")),
            str((name_info or {}).get("alias_name", "")),
            " ".join((name_info or {}).get("binding_names", []) or []),
        ],
    ).lower()
    is_depth = any(h in format_name for h in _DEPTH_HINTS) or any(h in names_blob for h in ("depth", "stencil"))
    is_hdr = any(h in format_name for h in _HDR_HINTS)
    is_compressed = any(h in format_name for h in _COMPRESSED_HINTS)
    is_normal = any(h in names_blob for h in _NORMAL_HINTS)
    is_mask = any(h in names_blob for h in _MASK_HINTS)
    is_color = any(h in names_blob for h in _COLOR_HINTS)
    is_cubemap = bool(getattr(texture_desc, "cubemap", False))
    array_size = int(getattr(texture_desc, "arraysize", getattr(texture_desc, "arraySize", 1)) or 1)
    if array_size >= 6 and "cube" in str(getattr(texture_desc, "type", "")).lower():
        is_cubemap = True

    if for_screenshot:
        if is_hdr or is_cubemap:
            return ["png", "exr", "hdr", "jpg"]
        return ["png", "jpg"]

    if is_depth:
        return ["dds", "raw", "png"]
    if is_hdr or is_cubemap:
        return ["dds", "exr", "hdr", "raw", "png"]
    if is_normal or is_mask:
        return ["png", "tga", "bmp", "dds"]
    if is_compressed and not is_color:
        return ["dds", "png", "tga"]
    return ["png", "jpg", "tga", "bmp", "dds"]


def _select_export_formats(
    requested_formats: Sequence[str],
    *,
    recommended_formats: Sequence[str],
) -> List[str]:
    requested = [_normalize_export_format(item) for item in requested_formats if str(item).strip()]
    if not requested:
        requested = ["png"]
    recommended = [_normalize_export_format(item) for item in recommended_formats if str(item).strip()]
    if not recommended:
        recommended = ["png"]

    if len(requested) == 1 and requested[0] in {"auto", "smart"}:
        return [recommended[0]]
    if len(requested) == 1 and requested[0] in {"all", "*"}:
        return list(dict.fromkeys(recommended))

    if any(item in {"all", "*"} for item in requested):
        for item in recommended:
            if item not in requested:
                requested.append(item)

    selected: List[str] = []
    for item in requested:
        if item in {"auto", "smart", "all", "*"}:
            continue
        if item not in selected:
            selected.append(item)
    return selected or [recommended[0]]


def _resolve_export_output_path(
    base_output_path: Optional[Any],
    *,
    name_stem: str,
    file_format: str,
    multi: bool,
) -> Optional[str]:
    if not base_output_path:
        return None
    fmt = _normalize_export_format(file_format)
    suffix = _FILE_SUFFIX_MAP.get(fmt, f".{fmt}")
    raw = str(base_output_path)
    path = Path(raw)
    is_dir_hint = raw.endswith(("\\", "/")) or path.is_dir() or not path.suffix

    if is_dir_hint:
        output_dir = path
        output_dir.mkdir(parents=True, exist_ok=True)
        return str(output_dir / f"{name_stem}{suffix}")

    if multi:
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path.parent / f"{_safe_name_token(path.stem)}_{fmt}{suffix}")

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() != suffix:
        path = path.with_suffix(suffix)
    return str(path)


def _flatten_actions(actions: Sequence[Any], out: Optional[List[Any]] = None) -> List[Any]:
    if out is None:
        out = []
    for action in actions:
        out.append(action)
        children = getattr(action, "children", None) or []
        _flatten_actions(children, out)
    return out


def _pick_default_event_id(actions: Sequence[Any]) -> int:
    flat = _flatten_actions(actions)
    for action in flat:
        flags = _map_action_flags(getattr(action, "flags", 0))
        if flags.get("is_draw") or flags.get("is_dispatch"):
            return int(getattr(action, "eventId", 0))
    return int(getattr(flat[0], "eventId", 0)) if flat else 0


def _action_name(action: Any) -> str:
    return str(getattr(action, "customName", "") or getattr(action, "name", "") or "")


def _map_action_flags(flags: Any) -> Dict[str, bool]:
    try:
        rd = _get_rd()
        af = rd.ActionFlags
    except Exception:
        return {}

    def _hf(name: str) -> bool:
        member = getattr(af, name, None)
        if member is None:
            return False
        try:
            return bool(flags & member)
        except Exception:
            return False

    return {
        "is_draw": _hf("Drawcall") or _hf("Draw"),
        "is_dispatch": _hf("Dispatch") or _hf("MeshDispatch") or _hf("DispatchRay"),
        "is_marker": _hf("SetMarker") or _hf("PushMarker") or _hf("PopMarker"),
        "is_copy": _hf("Copy"),
        "is_resolve": _hf("Resolve"),
        "is_clear": _hf("Clear"),
        "is_pass_boundary": _hf("Present") or _hf("PassBoundary"),
    }


def _action_to_dict(action: Any, *, include_children: bool = True, depth: int = 0) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "event_id": int(getattr(action, "eventId", 0)),
        "name": _action_name(action),
        "flags": _map_action_flags(getattr(action, "flags", 0)),
        "num_indices": int(getattr(action, "numIndices", 0)),
        "num_instances": int(getattr(action, "numInstances", 0)),
        "num_vertices": int(getattr(action, "numIndices", 0)),
        "depth": depth,
    }
    outputs = getattr(action, "outputs", None) or []
    if outputs:
        payload["outputs"] = [str(o) for o in outputs]
    if include_children:
        children = getattr(action, "children", None) or []
        payload["children"] = [_action_to_dict(c, include_children=True, depth=depth + 1) for c in children]
    return payload


def _artifact_path(artifact_ref: Any) -> Optional[str]:
    if artifact_ref is None or _artifact_store is None:
        return None
    sha256 = getattr(artifact_ref, "sha256", None)
    if not sha256:
        return None
    return str(_artifact_store.get_path(sha256))


def _format_size(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f}{unit}"
        size /= 1024.0
    return f"{value}B"


def _sanitize_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def _parse_stage(stage: Optional[str]) -> str:
    if not stage:
        return "ps"
    return str(stage).strip().lower()


def _rd_stage(stage: str) -> Any:
    rd = _get_rd()
    mapping = {
        "vs": rd.ShaderStage.Vertex,
        "hs": rd.ShaderStage.Hull,
        "ds": rd.ShaderStage.Domain,
        "gs": rd.ShaderStage.Geometry,
        "ps": rd.ShaderStage.Pixel,
        "cs": rd.ShaderStage.Compute,
        "ms": getattr(rd.ShaderStage, "Mesh", rd.ShaderStage.Compute),
        "as": getattr(rd.ShaderStage, "Amplification", rd.ShaderStage.Compute),
    }
    return mapping.get(stage.lower(), rd.ShaderStage.Pixel)


def _stage_candidates() -> List[str]:
    return ["vs", "hs", "ds", "gs", "ps", "cs", "ms", "as"]


async def _get_controller(session_id: str) -> Any:
    assert _session_manager is not None
    return _session_manager.get_controller(session_id)


def _get_replay_handle(session_id: str) -> ReplayHandle:
    replay = _runtime.replays.get(session_id)
    if replay is None:
        raise ValueError(f"Unknown replay session_id: {session_id}")
    return replay


def _active_event(session_id: str) -> int:
    replay = _runtime.replays.get(session_id)
    if replay is None:
        return 0
    return replay.active_event_id


async def _ensure_event(session_id: str, event_id: Optional[int]) -> int:
    controller = await _get_controller(session_id)
    if event_id is None:
        event = _active_event(session_id)
        if event <= 0:
            roots = await _offload(controller.GetRootActions)
            event = _pick_default_event_id(roots)
    else:
        event = int(event_id)
    if event > 0:
        await _offload(controller.SetFrameEvent, event, True)
        if session_id in _runtime.replays:
            _runtime.replays[session_id].active_event_id = event
    return event


async def _resolve_resource_id(session_id: str, resource_id: Any) -> Any:
    controller = await _get_controller(session_id)
    wanted = str(resource_id)
    textures = await _offload(controller.GetTextures)
    buffers = await _offload(controller.GetBuffers)
    resources = await _offload(controller.GetResources)
    for collection in (textures, buffers, resources):
        for obj in collection:
            rid = getattr(obj, "resourceId", None)
            if rid is None:
                continue
            if wanted in _resource_keys(rid):
                return rid
    raise ValueError(f"Resource not found: {resource_id}")


async def _resolve_texture_id(session_id: str, texture_id: Optional[Any], *, event_id: Optional[int] = None) -> Any:
    controller = await _get_controller(session_id)
    if texture_id:
        return await _resolve_resource_id(session_id, texture_id)
    target_event = await _ensure_event(session_id, event_id)
    if target_event <= 0:
        raise ValueError("No active event to infer texture target")
    pipe = await _offload(controller.GetPipelineState)
    rd = _get_rd()
    null_id = rd.ResourceId()
    outputs = await _offload(pipe.GetOutputTargets)
    for out in reversed(outputs):
        rid = getattr(out, "resourceId", null_id)
        if rid != null_id:
            return rid
    textures = await _offload(controller.GetTextures)
    if not textures:
        raise ValueError("No textures available in capture")
    return textures[0].resourceId


async def _binding_name_index_for_event(session_id: str, event_id: Optional[int]) -> Dict[str, List[str]]:
    if _pipeline_service is None:
        return {}
    evt = _as_int(event_id, 0)
    if evt <= 0:
        try:
            evt = await _ensure_event(session_id, None)
        except Exception:
            evt = 0
    if evt <= 0:
        return {}
    try:
        bindings = await _pipeline_service.get_resource_bindings(session_id, evt, _session_manager)
    except Exception:
        return {}
    index: Dict[str, List[str]] = {}
    for binding in bindings:
        rid = str(getattr(binding, "resource_id", "")).strip()
        if not rid:
            continue
        label = str(getattr(binding, "resource_name", "")).strip()
        if not label:
            label = f"{str(getattr(binding, 'type', 'res')).lower()}{int(getattr(binding, 'binding', 0))}"
        for key in _resource_id_tokens(rid):
            bucket = index.setdefault(key, [])
            if label not in bucket:
                bucket.append(label)
    return index


async def _get_texture_descriptor(
    session_id: str,
    texture_id: Any,
    *,
    event_id: Optional[int] = None,
) -> Tuple[Any, Optional[Any]]:
    controller = await _get_controller(session_id)
    resolved = await _resolve_texture_id(session_id, texture_id, event_id=event_id)
    textures = await _offload(controller.GetTextures)
    for texture in textures:
        rid = getattr(texture, "resourceId", None)
        if rid is not None and _resource_id_matches(rid, resolved):
            return resolved, texture
    return resolved, None


def _extract_descriptor_resource_id(descriptor: Any) -> Any:
    rid = getattr(descriptor, "resourceId", None)
    if rid is not None:
        return rid
    resource = getattr(descriptor, "resource", None)
    if resource is None:
        return None
    return getattr(resource, "resourceId", resource)


async def _output_target_resource_ids(session_id: str, event_id: Optional[int]) -> List[Tuple[Any, int]]:
    controller = await _get_controller(session_id)
    evt = await _ensure_event(session_id, event_id)
    if evt <= 0:
        return []
    pipe = await _offload(controller.GetPipelineState)
    outputs = await _offload(pipe.GetOutputTargets)
    rd = _get_rd()
    null_id = rd.ResourceId()
    out: List[Tuple[Any, int]] = []
    for idx, desc in enumerate(outputs):
        rid = _extract_descriptor_resource_id(desc)
        if rid is None:
            continue
        if rid == null_id:
            continue
        out.append((rid, idx))
    return out


async def _pipeline_snapshot(session_id: str, event_id: Optional[int] = None) -> Any:
    assert _pipeline_service is not None
    evt = await _ensure_event(session_id, event_id)
    return await _pipeline_service.snapshot_pipeline(
        session_id=session_id,
        event_id=evt,
        session_manager=_session_manager,
    )


@asynccontextmanager
async def _lifespan(_: FastMCP):
    await runtime_startup()
    try:
        yield
    finally:
        await runtime_shutdown()


async def runtime_startup() -> None:
    global _config, _session_manager, _event_graph_service, _render_service
    global _pipeline_service, _perf_service
    global _artifact_store, _runtime_bootstrapped
    if _runtime_bootstrapped:
        return

    ensure_runtime_dirs()
    bootstrap = bootstrap_renderdoc_runtime(probe_import=False)
    renderdoc_dir = bootstrap.pymodules_dir
    if not (renderdoc_dir / "renderdoc.pyd").is_file():
        _record_log("warning", f"renderdoc runtime missing: {renderdoc_dir / 'renderdoc.pyd'}")
    for item in bootstrap.dll_dir_errors:
        _record_log("warning", f"renderdoc bootstrap warning: {item}")

    _config = RdxConfig.from_env()
    artifact_root = Path(os.environ.get("RDX_ARTIFACT_DIR", str(artifacts_dir()))).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    _artifact_store = ArtifactStore(root=artifact_root)

    _session_manager = SessionManager()
    _event_graph_service = EventGraphService()
    _render_service = RenderService()
    _pipeline_service = PipelineService()
    _perf_service = PerfService()

    _runtime.config = {
        "artifact_dir": str(artifact_root),
        "temp_dir": str(runtime_root().resolve()),
        "log_level": os.environ.get("RDX_LOG_LEVEL", "INFO").lower(),
    }
    _runtime.initialized = False
    _runtime.logs.clear()
    _runtime_bootstrapped = True
    _record_log("info", "RDX runtime initialized")
    _ensure_core_engine()


async def runtime_shutdown() -> None:
    global _runtime_bootstrapped
    if not _runtime_bootstrapped:
        return
    for debug_id in list(_runtime.shader_debugs.keys()):
        handle = _runtime.shader_debugs.pop(debug_id, None)
        if handle is not None:
            try:
                controller = _session_manager.get_controller(handle.session_id)
                controller.FreeTrace(handle.trace)
            except Exception:
                pass
    if _session_manager is not None:
        for info in list(_session_manager.list_sessions()):
            try:
                await _session_manager.close_session(info.session_id)
            except Exception:
                pass
    _runtime_bootstrapped = False
    _record_log("info", "RDX runtime shutdown complete")


def _ensure_core_engine() -> CoreEngine:
    global _operation_registry, _core_engine
    if _operation_registry is None:
        _operation_registry = OperationRegistry()
        _operation_registry.set_default(_core_operation_handler)
    if _core_engine is None:
        _core_engine = CoreEngine(
            registry=_operation_registry,
            artifact_publisher=ArtifactPublisher(),
        )
    return _core_engine


def get_core_engine() -> CoreEngine:
    return _ensure_core_engine()


def _create_mcp() -> FastMCP:
    kwargs: Dict[str, Any] = {}
    description = "RenderDoc MCP tools (196 doc tools)"
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


async def _core_operation_handler(args: Dict[str, Any], env: Dict[str, Any]) -> Dict[str, Any]:
    operation = str(env.get("operation", "rd.unknown.unknown"))
    raw = await _dispatch_tool_legacy(operation, args)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"success": False, "error_message": f"Non-JSON legacy output for {operation}"}
    if isinstance(raw, dict):
        return raw
    return {"success": False, "error_message": f"Unsupported legacy output type: {type(raw).__name__}"}


async def dispatch_operation(
    operation: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    transport: str = "core",
    remote: bool = False,
) -> Dict[str, Any]:
    await runtime_startup()
    engine = _ensure_core_engine()
    call_args = dict(args or {})
    ctx = ExecutionContext(transport=transport, remote=remote)
    arg_keys = ",".join(sorted(call_args.keys())) if call_args else "-"
    logger.info(
        "op.start transport=%s remote=%s op=%s trace_id=%s arg_keys=%s",
        transport,
        remote,
        operation,
        ctx.trace_id,
        arg_keys,
    )
    payload = await engine.execute(operation, call_args, context=ctx)
    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    logger.info(
        "op.done transport=%s op=%s trace_id=%s ok=%s duration_ms=%s",
        transport,
        operation,
        str(meta.get("trace_id") or ctx.trace_id),
        bool(payload.get("ok")) if isinstance(payload, dict) else False,
        meta.get("duration_ms"),
    )
    return payload


async def _dispatch_tool(tool_name: str, args: Dict[str, Any]) -> str:
    payload = await dispatch_operation(tool_name, args, transport="mcp", remote=tool_name.startswith("rd.remote."))
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


async def _dispatch_tool_legacy(tool_name: str, args: Dict[str, Any]) -> str:
    args = {k: _parse_json_like(v) for k, v in args.items() if v is not None}
    _record_log("debug", f"tool_call {tool_name}", {"args": sorted(args.keys())})
    try:
        parts = tool_name.split(".")
        if len(parts) != 3 or parts[0] != "rd":
            return _err(f"Invalid tool name: {tool_name}")
        domain, action = parts[1], parts[2]

        dispatcher = {
            "core": _dispatch_core,
            "capture": _dispatch_capture,
            "replay": _dispatch_replay,
            "event": _dispatch_event,
            "pipeline": _dispatch_pipeline,
            "resource": _dispatch_resource,
            "texture": _dispatch_texture,
            "buffer": _dispatch_buffer,
            "mesh": _dispatch_mesh,
            "shader": _dispatch_shader,
            "debug": _dispatch_debug,
            "perf": _dispatch_perf,
            "export": _dispatch_export,
            "diag": _dispatch_diag,
            "macro": _dispatch_macro,
            "analysis": _dispatch_analysis,
            "util": _dispatch_util,
            "remote": _dispatch_remote,
            "app": _dispatch_app,
        }.get(domain)

        if dispatcher is None:
            return _err(f"No dispatcher for domain '{domain}'")
        return await dispatcher(action, args)
    except SessionError as exc:
        return _err(exc.detail.message, code=exc.detail.code, details=exc.detail.details)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        _record_log(
            "debug",
            f"tool_validation_failed {tool_name}",
            {"error": str(exc)},
        )
        return _err(str(exc))
    except Exception as exc:
        logger.exception("Tool dispatch failed: %s", tool_name)
        return _err(str(exc))


async def _dispatch_core(action: str, args: Dict[str, Any]) -> str:
    if action == "init":
        global_env = _as_dict(args.get("global_env"), default={})
        _runtime.enable_remote = _as_bool(args.get("enable_remote"), True)
        _runtime.enable_app_api = _as_bool(args.get("enable_app_api"), False)
        _runtime.config.update(global_env)
        _runtime.initialized = True
        version = await _core_get_version_value()
        capabilities = await _core_capabilities(detail="summary")
        return _ok(api_version=version, capabilities=capabilities)

    if action == "shutdown":
        released = {
            "sessions": len(_runtime.replays),
            "capture_files": len(_runtime.captures),
            "remote_connections": len(_runtime.remotes),
            "shader_debugs": len(_runtime.shader_debugs),
        }
        for sid in list(_runtime.replays.keys()):
            try:
                await _session_manager.close_session(sid)
            except Exception:
                pass
        _runtime.replays.clear()
        _runtime.captures.clear()
        _runtime.shader_debugs.clear()
        _runtime.remotes.clear()
        _runtime.initialized = False
        return _ok(released=released)

    if action == "get_version":
        version = await _core_get_version_value()
        return _ok(version=version, commit_hash=None, build_date=None)

    if action == "get_capabilities":
        detail = str(args.get("detail_level", "summary"))
        return _ok(capabilities=await _core_capabilities(detail=detail))

    if action == "set_config":
        _require(args, "config")
        cfg = _as_dict(args.get("config"))
        _runtime.config.update(cfg)
        return _ok(applied=dict(_runtime.config))

    if action == "get_config":
        return _ok(config=dict(_runtime.config))

    if action == "set_log_level":
        level = str(args.get("level", "info")).upper()
        logging.getLogger().setLevel(level)
        _runtime.config["log_level"] = level.lower()
        return _ok()

    if action == "get_logs":
        since_ms = args.get("since_ms")
        level_min = str(args.get("level_min", "")).lower().strip()
        max_lines = _as_int(args.get("max_lines"), 500)
        levels = ["trace", "debug", "info", "warn", "warning", "error"]
        if level_min and level_min in levels:
            cutoff = levels.index("warning" if level_min == "warn" else level_min)
        else:
            cutoff = 0
        out: List[Dict[str, Any]] = []
        for item in _runtime.logs:
            if since_ms is not None and int(item.get("ts_ms", 0)) < int(since_ms):
                continue
            lv = str(item.get("level", "info")).lower()
            lv = "warning" if lv == "warn" else lv
            idx = levels.index(lv) if lv in levels else 0
            if idx < cutoff:
                continue
            out.append(item)
        return _ok(logs=out[-max_lines:])

    if action == "healthcheck":
        checks: List[Dict[str, Any]] = []
        try:
            rd = _get_rd()
            _ = rd.GetVersionString() if hasattr(rd, "GetVersionString") else "unknown"
            checks.append({"name": "renderdoc_import", "ok": True})
        except Exception as exc:
            checks.append({"name": "renderdoc_import", "ok": False, "detail": str(exc)})

        artifact_dir = Path(_runtime.config.get("artifact_dir", str(artifacts_dir())))
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            probe = artifact_dir / ".healthcheck.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            checks.append({"name": "artifact_dir_writable", "ok": True})
        except Exception as exc:
            checks.append({"name": "artifact_dir_writable", "ok": False, "detail": str(exc)})

        if _as_bool(args.get("check_replay"), True):
            checks.append({"name": "replay_runtime", "ok": True})
        if _as_bool(args.get("check_remote"), False):
            checks.append({"name": "remote_runtime", "ok": _runtime.enable_remote})
        success = all(bool(item.get("ok")) for item in checks)
        if not success:
            return _err("healthcheck failed", checks=checks)
        return _ok(checks=checks)

    return _err(f"Unsupported core action: {action}")


async def _core_get_version_value() -> str:
    try:
        rd = _get_rd()
        if hasattr(rd, "GetVersionString"):
            return str(rd.GetVersionString())
    except Exception:
        pass
    return "unknown"


async def _core_capabilities(*, detail: str) -> Dict[str, Any]:
    summary = {
        "replay": {"available": True},
        "remote": {"available": _runtime.enable_remote},
        "app_api": {"available": _runtime.enable_app_api},
        "shader_debug": {"available": True},
        "counters": {"available": True},
        "artifact_dir": _runtime.config.get("artifact_dir"),
    }
    if detail == "full":
        summary["sessions"] = len(_runtime.replays)
        summary["capture_files"] = len(_runtime.captures)
        summary["remote_connections"] = len(_runtime.remotes)
    return summary


async def _dispatch_capture(action: str, args: Dict[str, Any]) -> str:
    if action == "open_file":
        _require(args, "file_path")
        file_path = str(args["file_path"])
        read_only = _as_bool(args.get("read_only"), True)
        path = Path(file_path)
        if not path.is_file():
            return _err(f"Capture file not found: {file_path}")
        driver = ""
        try:
            rd = _get_rd()
            cap = await _offload(rd.OpenCaptureFile)
            status = await _offload(cap.OpenFile, str(path), "", None)
            _check_status(status, "OpenFile")
            try:
                status2, controller = await _offload(cap.OpenCapture, rd.ReplayOptions(), None)
                if status2 == rd.ResultCode.Succeeded:
                    props = await _offload(controller.GetAPIProperties)
                    driver = str(getattr(props, "pipelineType", ""))
                    await _offload(controller.Shutdown)
            finally:
                if hasattr(cap, "CloseFile"):
                    await _offload(cap.CloseFile)
                elif hasattr(cap, "Shutdown"):
                    await _offload(cap.Shutdown)
        except Exception:
            driver = ""

        capture_file_id = _new_id("capf")
        _runtime.captures[capture_file_id] = CaptureFileHandle(
            capture_file_id=capture_file_id,
            file_path=str(path),
            read_only=read_only,
            driver=driver,
        )
        return _ok(capture_file_id=capture_file_id, driver=driver)

    if action == "close_file":
        _require(args, "capture_file_id")
        _runtime.captures.pop(str(args["capture_file_id"]), None)
        return _ok()

    if action == "get_info":
        _require(args, "capture_file_id")
        handle = _runtime.captures.get(str(args["capture_file_id"]))
        if handle is None:
            return _err(f"Unknown capture_file_id: {args['capture_file_id']}")
        path = Path(handle.file_path)
        metadata = {
            "path": str(path),
            "name": path.name,
            "api": handle.driver,
            "size_bytes": int(path.stat().st_size) if path.exists() else 0,
            "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat() if path.exists() else None,
        }
        return _ok(metadata=_sanitize_dict(metadata))

    if action == "get_thumbnail":
        _require(args, "capture_file_id")
        handle = _runtime.captures.get(str(args["capture_file_id"]))
        if handle is None:
            return _err(f"Unknown capture_file_id: {args['capture_file_id']}")
        return _ok(image_path=None, width=None, height=None)

    if action == "list_frames":
        _require(args, "capture_file_id")
        handle = _runtime.captures.get(str(args["capture_file_id"]))
        if handle is None:
            return _err(f"Unknown capture_file_id: {args['capture_file_id']}")
        frames = [{"frame_index": 0, "timestamp": None, "has_thumbnail": False}]
        return _ok(frames=frames)

    if action == "open_replay":
        _require(args, "capture_file_id")
        capture_file_id = str(args["capture_file_id"])
        handle = _runtime.captures.get(capture_file_id)
        if handle is None:
            return _err(f"Unknown capture_file_id: {capture_file_id}")
        options = _as_dict(args.get("options"), default={})
        backend_type = "remote" if options.get("remote_id") else "local"
        session_info = await _session_manager.create_session(
            backend_config={"type": backend_type},
            replay_config={},
        )
        cap_info = await _session_manager.open_capture(session_info.session_id, handle.file_path)
        controller = await _get_controller(session_info.session_id)
        roots = await _offload(controller.GetRootActions)
        active_event_id = int(getattr(roots[0], "eventId", 0)) if roots else 0
        _runtime.replays[session_info.session_id] = ReplayHandle(
            session_id=session_info.session_id,
            capture_file_id=capture_file_id,
            frame_index=0,
            active_event_id=active_event_id,
        )
        api_properties = {}
        try:
            props = await _offload(controller.GetAPIProperties)
            api_properties = {"pipeline_type": str(getattr(props, "pipelineType", ""))}
        except Exception:
            api_properties = {}
        return _ok(
            session_id=session_info.session_id,
            frame_count=max(1, int(getattr(cap_info, "frame_count", 1))),
            api_properties=api_properties,
        )

    if action == "close_replay":
        _require(args, "session_id")
        session_id = str(args["session_id"])
        _runtime.replays.pop(session_id, None)
        await _session_manager.close_session(session_id)
        return _ok()

    return _err(f"Unsupported capture action: {action}")


async def _dispatch_replay(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])
    replay = _get_replay_handle(session_id)
    controller = await _get_controller(session_id)

    if action == "set_frame":
        replay.frame_index = _as_int(args.get("frame_index"), 0)
        roots = await _offload(controller.GetRootActions)
        active_event_id = _pick_default_event_id(roots)
        if active_event_id > 0:
            await _offload(controller.SetFrameEvent, active_event_id, True)
        replay.active_event_id = active_event_id
        return _ok(active_event_id=active_event_id)

    if action == "get_frame_info":
        roots = await _offload(controller.GetRootActions)
        flat = _flatten_actions(roots)
        drawcalls = 0
        markers = 0
        for action_obj in flat:
            flags = _map_action_flags(getattr(action_obj, "flags", 0))
            if flags.get("is_draw"):
                drawcalls += 1
            if flags.get("is_marker"):
                markers += 1
        frame_info = {
            "frame_index": replay.frame_index,
            "event_range": {
                "start": int(getattr(flat[0], "eventId", 0)) if flat else 0,
                "end": int(getattr(flat[-1], "eventId", 0)) if flat else 0,
            },
            "drawcall_count": drawcalls,
            "marker_count": markers,
        }
        return _ok(frame_info=frame_info)

    if action == "get_api_properties":
        props = await _offload(controller.GetAPIProperties)
        api_properties = {
            "pipeline_type": str(getattr(props, "pipelineType", "")),
            "local_renderer": str(getattr(props, "localRenderer", "")),
            "shader_debugging": bool(getattr(props, "shaderDebugging", False)),
        }
        return _ok(api_properties=api_properties)

    if action == "get_driver_info":
        props = await _offload(controller.GetAPIProperties)
        info = {
            "vendor": str(getattr(props, "vendor", "")),
            "device": str(getattr(props, "localRenderer", "")),
            "driver_version": str(getattr(props, "driverVersion", "")),
            "driver_name": str(getattr(props, "localRenderer", "")),
            "replay_api": str(getattr(props, "pipelineType", "")),
        }
        return _ok(driver_info=_sanitize_dict(info))

    return _err(f"Unsupported replay action: {action}")


async def _dispatch_event(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])
    controller = await _get_controller(session_id)

    roots = await _offload(controller.GetRootActions)
    flat = _flatten_actions(roots)
    by_event = {int(getattr(a, "eventId", 0)): a for a in flat}

    def _parent_chain(event_id: int) -> List[Any]:
        chain: List[Any] = []

        def walk(nodes: Sequence[Any], stack: List[Any]) -> bool:
            for node in nodes:
                stack.append(node)
                if int(getattr(node, "eventId", 0)) == event_id:
                    chain.extend(stack)
                    return True
                children = getattr(node, "children", None) or []
                if walk(children, stack):
                    return True
                stack.pop()
            return False

        walk(roots, [])
        return chain

    if action == "set_active":
        _require(args, "event_id")
        event_id = _as_int(args["event_id"])
        await _offload(controller.SetFrameEvent, event_id, True)
        if session_id in _runtime.replays:
            _runtime.replays[session_id].active_event_id = event_id
        return _ok(active_event_id=event_id)

    if action == "get_active":
        return _ok(active_event_id=_active_event(session_id))

    if action == "get_actions":
        include_markers = _as_bool(args.get("include_markers"), True)
        include_drawcalls = _as_bool(args.get("include_drawcalls"), True)
        out = []
        for root in roots:
            item = _action_to_dict(root, include_children=True, depth=0)
            flags = item.get("flags", {})
            if flags.get("is_marker") and not include_markers:
                continue
            if flags.get("is_draw") and not include_drawcalls:
                continue
            out.append(item)
        return _ok(actions=out)

    if action == "get_action_tree":
        max_depth = args.get("max_depth")
        filter_cfg = _as_dict(args.get("filter"), default={})
        name_contains = str(filter_cfg.get("name_contains", "")).strip().lower()

        def trim(node: Dict[str, Any], depth: int) -> Optional[Dict[str, Any]]:
            if max_depth is not None and depth > int(max_depth):
                return None
            if name_contains and name_contains not in str(node.get("name", "")).lower():
                pass
            children = [trim(c, depth + 1) for c in node.get("children", [])]
            node["children"] = [c for c in children if c is not None]
            return node

        root_payload = {"event_id": 0, "name": "root", "flags": {}, "children": []}
        for r in roots:
            node = trim(_action_to_dict(r, include_children=True, depth=1), 1)
            if node is not None:
                root_payload["children"].append(node)
        return _ok(root=root_payload)

    if action == "get_action_details":
        _require(args, "event_id")
        event_id = _as_int(args["event_id"])
        action_obj = by_event.get(event_id)
        if action_obj is None:
            return _err(f"Event not found: {event_id}")
        payload = _action_to_dict(action_obj, include_children=True)
        return _ok(action=payload)

    if action == "get_drawcall_children":
        _require(args, "event_id")
        event_id = _as_int(args["event_id"])
        action_obj = by_event.get(event_id)
        if action_obj is None:
            return _err(f"Event not found: {event_id}")
        children = getattr(action_obj, "children", None) or []
        ids = [int(getattr(c, "eventId", 0)) for c in children]
        return _ok(children_event_ids=ids)

    if action == "get_parent_chain":
        _require(args, "event_id")
        event_id = _as_int(args["event_id"])
        chain = _parent_chain(event_id)
        out = [
            {
                "event_id": int(getattr(item, "eventId", 0)),
                "name": _action_name(item),
                "flags": _map_action_flags(getattr(item, "flags", 0)),
            }
            for item in chain
        ]
        return _ok(parent_chain=out)

    if action == "search_actions":
        query = _parse_query_like(args.get("query"))
        max_results = _as_int(args.get("max_results"), 200)
        name_regex = query.get("name_regex")
        pattern = re.compile(str(name_regex)) if name_regex else None
        name_contains = str(query.get("name_contains", "")).lower().strip()
        event_id_min = query.get("event_id_min")
        event_id_max = query.get("event_id_max")
        matches = []
        for action_obj in flat:
            eid = int(getattr(action_obj, "eventId", 0))
            name = _action_name(action_obj)
            if event_id_min is not None and eid < int(event_id_min):
                continue
            if event_id_max is not None and eid > int(event_id_max):
                continue
            if name_contains and name_contains not in name.lower():
                continue
            if pattern and not pattern.search(name):
                continue
            chain = _parent_chain(eid)
            matches.append(
                {
                    "event_id": eid,
                    "name": name,
                    "flags": _map_action_flags(getattr(action_obj, "flags", 0)),
                    "path": [_action_name(item) for item in chain],
                },
            )
            if len(matches) >= max_results:
                break
        return _ok(matches=matches)

    if action == "list_passes":
        tree = _event_graph_service.build_event_tree(session_id, _session_manager)
        tree = _event_graph_service.infer_passes(tree, session_id, _session_manager)
        pass_map: Dict[str, Dict[str, Any]] = {}

        def walk(nodes: List[Any]) -> None:
            for node in nodes:
                if node.inferred_pass:
                    slot = pass_map.setdefault(
                        node.inferred_pass,
                        {
                            "name": node.inferred_pass,
                            "begin_event_id": node.event_id,
                            "end_event_id": node.event_id,
                            "drawcall_count": 0,
                        },
                    )
                    slot["begin_event_id"] = min(slot["begin_event_id"], node.event_id)
                    slot["end_event_id"] = max(slot["end_event_id"], node.event_id)
                    if node.flags.is_draw:
                        slot["drawcall_count"] += 1
                walk(node.children)

        walk(tree)
        passes = list(pass_map.values())
        passes.sort(key=lambda p: p["begin_event_id"])
        return _ok(passes=passes)

    if action == "get_marker_stack":
        _require(args, "event_id")
        event_id = _as_int(args["event_id"])
        chain = _parent_chain(event_id)
        stack = [_action_name(item) for item in chain if _map_action_flags(getattr(item, "flags", 0)).get("is_marker")]
        return _ok(stack=stack)

    if action == "get_api_calls":
        return _ok(api_calls=[])

    if action == "get_callstack":
        _require(args, "event_id")
        event_id = _as_int(args["event_id"])
        action_obj = by_event.get(event_id)
        if action_obj is None:
            return _err(f"Event not found: {event_id}")
        callstack = []
        raw = getattr(action_obj, "callstack", None) or []
        for frame in raw:
            callstack.append(
                {
                    "module": str(getattr(frame, "module", "")),
                    "function": str(getattr(frame, "function", "")),
                    "file": str(getattr(frame, "file", "")),
                    "line": int(getattr(frame, "line", 0)),
                    "address": str(getattr(frame, "address", "")),
                },
            )
        return _ok(callstack=callstack)

    if action == "diff_pipeline_state":
        _require(args, "event_a", "event_b")
        event_a = _as_int(args["event_a"])
        event_b = _as_int(args["event_b"])
        snap_a = await _pipeline_service.snapshot_pipeline(session_id, event_a, _session_manager)
        snap_b = await _pipeline_service.snapshot_pipeline(session_id, event_b, _session_manager)
        a = snap_a.model_dump(mode="json")
        b = snap_b.model_dump(mode="json")
        diff: List[Dict[str, Any]] = []

        def walk(path: str, va: Any, vb: Any) -> None:
            if isinstance(va, dict) and isinstance(vb, dict):
                keys = set(va.keys()) | set(vb.keys())
                for k in sorted(keys):
                    walk(f"{path}.{k}" if path else k, va.get(k), vb.get(k))
                return
            if isinstance(va, list) and isinstance(vb, list):
                max_len = max(len(va), len(vb))
                for i in range(max_len):
                    walk(f"{path}[{i}]", va[i] if i < len(va) else None, vb[i] if i < len(vb) else None)
                return
            if va != vb:
                diff.append({"path": path, "before": va, "after": vb})

        walk("", a, b)
        return _ok(diff=diff)

    if action == "get_resource_usage":
        event_id = args.get("event_id")
        snap = await _pipeline_snapshot(session_id, event_id=_as_int(event_id) if event_id is not None else None)
        usage = {
            "render_targets": [rt.model_dump(mode="json") for rt in snap.render_targets],
            "depth_target": snap.depth_target.model_dump(mode="json") if snap.depth_target else None,
            "bindings": [b.model_dump(mode="json") for b in snap.bindings],
        }
        return _ok(usage=usage)

    return _err(f"Unsupported event action: {action}")


async def _dispatch_pipeline(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])
    stage = _parse_stage(args.get("stage"))
    snapshot = await _pipeline_snapshot(session_id, event_id=None)
    snapshot_dict = snapshot.model_dump(mode="json")
    controller = await _get_controller(session_id)
    event_id = _active_event(session_id)
    if event_id > 0:
        await _offload(controller.SetFrameEvent, event_id, True)
    pipe = await _offload(controller.GetPipelineState)

    if action == "get_state":
        return _ok(pipeline_state=snapshot_dict)
    if action == "get_state_summary":
        summary = {
            "api": snapshot_dict.get("api"),
            "shaders": snapshot_dict.get("shaders", []),
            "render_targets": snapshot_dict.get("render_targets", []),
            "binding_count": len(snapshot_dict.get("bindings", [])),
            "topology": snapshot_dict.get("topology", ""),
            "viewport": snapshot_dict.get("viewport", {}),
        }
        return _ok(summary=summary)
    if action == "get_stage_state":
        rd_stage = _rd_stage(stage)
        shader_id = await _offload(pipe.GetShader, rd_stage)
        reflection = await _offload(pipe.GetShaderReflection, rd_stage)
        state = {
            "stage": stage.upper(),
            "shader_id": str(shader_id),
            "entry": str(getattr(reflection, "entryPoint", "")) if reflection else "",
            "resources": [],
            "samplers": [],
            "constant_blocks": [],
        }
        return _ok(stage_state=state)
    if action == "get_vertex_input":
        return _ok(ia={"topology": snapshot_dict.get("topology"), "vertex_buffers": snapshot_dict.get("bindings", [])})
    if action == "get_vertex_buffers":
        vbs = []
        try:
            raw = await _offload(pipe.GetVBuffers)
            for idx, vb in enumerate(raw):
                vbs.append(
                    {
                        "slot": idx,
                        "resource_id": str(getattr(vb, "resourceId", "")),
                        "offset": int(getattr(vb, "byteOffset", 0)),
                        "stride": int(getattr(vb, "byteStride", 0)),
                    },
                )
        except Exception:
            pass
        return _ok(vertex_buffers=vbs)
    if action == "get_index_buffer":
        index_buffer = {}
        try:
            ib = await _offload(pipe.GetIBuffer)
            index_buffer = {
                "resource_id": str(getattr(ib, "resourceId", "")),
                "offset": int(getattr(ib, "byteOffset", 0)),
                "format": str(getattr(ib, "byteStride", "")),
            }
        except Exception:
            index_buffer = {}
        return _ok(index_buffer=index_buffer)
    if action == "get_primitive_topology":
        return _ok(topology={"topology": snapshot_dict.get("topology", "")})
    if action == "get_viewports_scissors":
        return _ok(viewports=[snapshot_dict.get("viewport", {})], scissors=[snapshot_dict.get("scissor", {})])
    if action == "get_rasterizer_state":
        return _ok(rasterizer={})
    if action == "get_multisample_state":
        return _ok(multisample={})
    if action == "get_blend_state":
        return _ok(blend={"states": snapshot_dict.get("blend_states", [])})
    if action == "get_depth_stencil_state":
        return _ok(depth_stencil=snapshot_dict.get("depth_stencil", {}))
    if action == "get_output_targets":
        return _ok(framebuffer={"render_targets": snapshot_dict.get("render_targets", []), "depth_target": snapshot_dict.get("depth_target")})
    if action == "get_render_targets":
        return _ok(render_targets=snapshot_dict.get("render_targets", []))
    if action == "get_depth_target":
        return _ok(depth_target=snapshot_dict.get("depth_target"))
    if action == "get_resource_bindings":
        bindings = await _pipeline_service.get_resource_bindings(session_id, _active_event(session_id), _session_manager)
        return _ok(bindings=[b.model_dump(mode="json") for b in bindings])
    if action == "get_uav_bindings":
        all_bindings = await _pipeline_service.get_resource_bindings(session_id, _active_event(session_id), _session_manager)
        uavs = [b.model_dump(mode="json") for b in all_bindings if b.type.upper() == "UAV"]
        return _ok(uavs=uavs)
    if action == "get_sampler_bindings":
        return _ok(samplers=[])
    if action == "get_constant_buffers":
        return _ok(constant_buffers=[])
    if action == "get_push_constants":
        return _ok(push_constants=[])
    if action == "get_dynamic_state":
        return _ok(dynamic_state={})
    if action == "get_root_signature":
        return _ok(root_signature={})
    if action == "get_descriptor_heaps":
        return _ok(descriptor_heaps=[])
    if action == "get_resource_states":
        return _ok(resource_states=[])
    if action == "get_shader":
        rd_stage = _rd_stage(stage)
        shader_id = await _offload(pipe.GetShader, rd_stage)
        reflection = await _offload(pipe.GetShaderReflection, rd_stage)
        shader = {
            "stage": stage.upper(),
            "shader_id": str(shader_id),
            "entry": str(getattr(reflection, "entryPoint", "")) if reflection else "",
            "encoding": str(getattr(reflection, "encoding", "")) if reflection else "",
        }
        return _ok(shader=shader)
    return _err(f"Unsupported pipeline action: {action}")


async def _dispatch_resource(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])
    controller = await _get_controller(session_id)
    binding_index_cache: Optional[Dict[str, List[str]]] = None

    async def get_binding_index() -> Dict[str, List[str]]:
        nonlocal binding_index_cache
        if binding_index_cache is None:
            binding_index_cache = await _binding_name_index_for_event(
                session_id,
                _active_event(session_id),
            )
        return binding_index_cache

    async def list_textures() -> List[Dict[str, Any]]:
        textures = await _offload(controller.GetTextures)
        binding_index = await get_binding_index()
        out = []
        for tex in textures:
            rid = getattr(tex, "resourceId", None)
            rid_text = str(rid)
            binding_names = list(binding_index.get(rid_text, []))
            alias_name = _runtime.aliases.get(rid_text, "")
            name_info = _compose_texture_name_info(
                rid_text,
                resource_name=str(getattr(tex, "name", "")),
                binding_names=binding_names,
                alias_name=alias_name,
            )
            out.append(
                {
                    "resource_id": rid_text,
                    "texture_id": rid_text,
                    "name": name_info["display_name"],
                    "resource_name": name_info["resource_name"],
                    "alias_name": name_info["alias_name"],
                    "binding_names": name_info["binding_names"],
                    "name_stem": name_info["name_stem"],
                    "width": int(getattr(tex, "width", 0)),
                    "height": int(getattr(tex, "height", 0)),
                    "depth": int(getattr(tex, "depth", 0)),
                    "mips": int(getattr(tex, "mips", 1)),
                    "format": str(getattr(getattr(tex, "format", None), "Name", lambda: str(getattr(tex, "format", "")))()),
                },
            )
        return out

    async def list_buffers() -> List[Dict[str, Any]]:
        buffers = await _offload(controller.GetBuffers)
        out = []
        for buf in buffers:
            rid = getattr(buf, "resourceId", None)
            rid_text = str(rid)
            out.append(
                {
                    "resource_id": rid_text,
                    "buffer_id": rid_text,
                    "name": _runtime.aliases.get(rid_text, str(getattr(buf, "name", ""))),
                    "length": int(getattr(buf, "length", 0)),
                    "byte_size": int(getattr(buf, "length", 0)),
                },
            )
        return out

    if action == "list_all":
        textures = await list_textures()
        buffers = await list_buffers()
        return _ok(resources=textures + buffers)
    if action == "list_textures":
        return _ok(textures=await list_textures())
    if action == "list_buffers":
        return _ok(buffers=await list_buffers())
    if action == "get_details":
        _require(args, "resource_id")
        rid = str(args["resource_id"])
        resources = (await list_textures()) + (await list_buffers())
        for item in resources:
            if item.get("resource_id") == rid:
                return _ok(details=item)
        return _err(f"Resource not found: {rid}")
    if action == "get_usage":
        _require(args, "resource_id")
        rid = await _resolve_resource_id(session_id, args["resource_id"])
        usage_raw = await _offload(controller.GetUsage, rid)
        usage = []
        for entry in usage_raw:
            usage.append(
                {
                    "event_id": int(getattr(entry, "eventId", 0)),
                    "usage": str(getattr(entry, "usage", "")),
                },
            )
        return _ok(usage=usage[: _as_int(args.get("max_events"), 10000)])
    if action == "get_history":
        _require(args, "resource_id")
        rid = await _resolve_resource_id(session_id, args["resource_id"])
        usage_raw = await _offload(controller.GetUsage, rid)
        history = []
        for entry in usage_raw:
            history.append(
                {
                    "event_id": int(getattr(entry, "eventId", 0)),
                    "usage": str(getattr(entry, "usage", "")),
                    "is_write": "Write" in str(getattr(entry, "usage", "")),
                },
            )
        return _ok(history=history)
    if action in {"get_initial_contents", "get_current_contents"}:
        _require(args, "resource_id")
        rid = str(args["resource_id"])
        textures = await list_textures()
        buffers = await list_buffers()
        if any(t["resource_id"] == rid for t in textures):
            output_path = args.get("output_path")
            if output_path:
                response = await _dispatch_texture(
                    "save_to_file",
                    {
                        "session_id": session_id,
                        "texture_id": rid,
                        "subresource": args.get("subresource"),
                        "output_path": output_path,
                        "file_format": args.get("file_format", "raw"),
                        "event_id": args.get("event_id"),
                    },
                )
                payload = json.loads(response)
                if payload.get("success"):
                    key = "initial_contents" if action == "get_initial_contents" else "current_contents"
                    contents: Dict[str, Any] = {
                        "artifact_path": payload.get("artifact_path"),
                        "saved_path": payload.get("saved_path"),
                        "meta": payload.get("meta"),
                    }
                    if payload.get("exports"):
                        contents["exports"] = payload.get("exports")
                        contents["saved_paths"] = payload.get("saved_paths")
                    return _ok(
                        **{
                            key: contents,
                        },
                    )
                return response
            response = await _dispatch_texture(
                "get_data",
                {
                    "session_id": session_id,
                    "texture_id": rid,
                    "subresource": args.get("subresource"),
                },
            )
            payload = json.loads(response)
            if payload.get("success"):
                key = "initial_contents" if action == "get_initial_contents" else "current_contents"
                return _ok(**{key: {"artifact_path": payload.get("artifact_path"), "stats": payload.get("stats")}})
            return response
        if any(b["resource_id"] == rid for b in buffers):
            response = await _dispatch_buffer(
                "get_data",
                {
                    "session_id": session_id,
                    "buffer_id": rid,
                    "offset": 0,
                    "size": args.get("range", {}).get("size") if isinstance(args.get("range"), dict) else None,
                    "output_path": args.get("output_path"),
                },
            )
            payload = json.loads(response)
            if payload.get("success"):
                key = "initial_contents" if action == "get_initial_contents" else "current_contents"
                return _ok(**{key: {"artifact_path": payload.get("artifact_path"), "byte_size": payload.get("byte_size")}})
            return response
        return _err(f"Resource not found: {rid}")
    if action == "set_alias":
        _require(args, "resource_id", "alias")
        _runtime.aliases[str(args["resource_id"])] = str(args["alias"])
        return _ok()
    if action == "rename":
        _require(args, "resource_id", "new_name")
        _runtime.aliases[str(args["resource_id"])] = str(args["new_name"])
        return _ok()
    if action == "get_descriptor_info":
        _require(args, "resource_id")
        details_response = await _dispatch_resource("get_details", {"session_id": session_id, "resource_id": args["resource_id"]})
        payload = json.loads(details_response)
        if payload.get("success"):
            return _ok(descriptor_info=payload.get("details"))
        return details_response
    if action == "estimate_memory":
        if args.get("resource_id") is not None:
            details_response = await _dispatch_resource("get_details", {"session_id": session_id, "resource_id": args["resource_id"]})
            payload = json.loads(details_response)
            if not payload.get("success"):
                return details_response
            details = payload.get("details", {})
            bytes_est = int(details.get("byte_size") or (details.get("width", 0) * details.get("height", 0) * 4))
            return _ok(memory={"bytes": bytes_est, "human": _format_size(bytes_est)})
        textures = await list_textures()
        buffers = await list_buffers()
        total = sum(int(t.get("width", 0) * t.get("height", 0) * 4) for t in textures) + sum(int(b.get("byte_size", 0)) for b in buffers)
        return _ok(memory={"bytes": total, "human": _format_size(total)})
    if action == "get_creation_context":
        _require(args, "resource_id")
        history_resp = await _dispatch_resource("get_history", {"session_id": session_id, "resource_id": args["resource_id"]})
        payload = json.loads(history_resp)
        if not payload.get("success"):
            return history_resp
        history = payload.get("history", [])
        first_event = history[0]["event_id"] if history else None
        return _ok(creation_context={"first_seen_event_id": first_event})
    return _err(f"Unsupported resource action: {action}")


async def _dispatch_buffer(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id", "buffer_id")
    session_id = str(args["session_id"])
    buffer_id = args["buffer_id"]
    controller = await _get_controller(session_id)
    rid = await _resolve_resource_id(session_id, buffer_id)
    offset = _as_int(args.get("offset"), 0)
    size = args.get("size")
    if size is None:
        size = 0
    size = _as_int(size, 0)
    data = await _offload(controller.GetBufferData, rid, offset, size)

    if action == "get_data":
        result: Dict[str, Any] = {"byte_size": len(data)}
        if _as_bool(args.get("as_base64"), False):
            import base64

            result["base64"] = base64.b64encode(data).decode("ascii")
        if args.get("output_path"):
            out = Path(str(args["output_path"]))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            result["artifact_path"] = str(out)
        else:
            assert _artifact_store is not None
            artifact = await _artifact_store.store(data, mime="application/octet-stream", suffix=".bin")
            result["artifact_path"] = _artifact_path(artifact)
        return _ok(**result)

    if action == "save_to_file":
        out = Path(str(args.get("output_path")))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return _ok(saved_path=str(out))

    if action == "search_pattern":
        pattern_value = args.get("pattern")
        if pattern_value is None:
            return _err("Missing required parameter(s): pattern")
        if isinstance(pattern_value, str):
            p = pattern_value.strip().replace(" ", "")
            if p.startswith("0x"):
                p = p[2:]
            pattern = bytes.fromhex(p)
        elif isinstance(pattern_value, list):
            pattern = bytes(int(v) & 0xFF for v in pattern_value)
        else:
            return _err("Unsupported pattern type")
        max_results = _as_int(args.get("max_results"), 256)
        matches = []
        start = 0
        while True:
            idx = data.find(pattern, start)
            if idx < 0:
                break
            matches.append({"offset": offset + idx})
            if len(matches) >= max_results:
                break
            start = idx + 1
        return _ok(matches=matches)

    if action == "get_structured_data":
        layout = _as_dict(args.get("layout"), default={})
        fields = _as_list(layout.get("fields"), default=[])
        stride = _as_int(layout.get("stride"), 0)
        if stride <= 0:
            return _ok(elements=[])
        offset_items = _as_int(args.get("offset"), 0)
        max_elements = _as_int(args.get("max_elements"), 256)
        count = _as_int(args.get("count"), max_elements)
        total = min(count, max_elements)
        type_map = {
            "u32": ("<I", 4),
            "i32": ("<i", 4),
            "f32": ("<f", 4),
            "u16": ("<H", 2),
            "i16": ("<h", 2),
            "u8": ("<B", 1),
            "i8": ("<b", 1),
        }
        elements = []
        for i in range(total):
            base = offset_items + i * stride
            if base + stride > len(data):
                break
            item: Dict[str, Any] = {"index": i}
            for field_def in fields:
                fd = _as_dict(field_def)
                name = str(fd.get("name", "field"))
                ftype = str(fd.get("type", "u32")).lower()
                fmt, size_bytes = type_map.get(ftype, ("<I", 4))
                field_offset = _as_int(fd.get("offset"), 0)
                start = base + field_offset
                end = start + size_bytes
                if end > len(data):
                    item[name] = None
                    continue
                item[name] = struct.unpack(fmt, data[start:end])[0]
            elements.append(item)
        return _ok(elements=elements)

    return _err(f"Unsupported buffer action: {action}")


async def _dispatch_mesh(action: str, args: Dict[str, Any]) -> str:
    if action == "get_drawcall_mesh_config":
        _require(args, "session_id", "event_id")
        session_id = str(args["session_id"])
        event_id = _as_int(args["event_id"])
        snap = await _pipeline_service.snapshot_pipeline(session_id, event_id, _session_manager)
        return _ok(mesh_config={"event_id": event_id, "topology": snap.topology, "bindings": [b.model_dump(mode="json") for b in snap.bindings]})
    if action in {"get_post_vs_data", "get_post_gs_data"}:
        return _err("Post-VS/GS extraction is not available in this build")
    if action == "decode_vertex_data":
        _require(args, "session_id", "vertex_buffer_id", "layout")
        buffer_response = await _dispatch_buffer(
            "get_structured_data",
            {
                "session_id": args["session_id"],
                "buffer_id": args["vertex_buffer_id"],
                "layout": args["layout"],
                "offset": args.get("vertex_offset", 0),
                "count": args.get("vertex_count", 128),
                "max_elements": args.get("vertex_count", 128),
            },
        )
        payload = json.loads(buffer_response)
        if not payload.get("success"):
            return buffer_response
        return _ok(vertices=payload.get("elements", []))
    if action == "decode_index_data":
        _require(args, "session_id", "index_buffer_id")
        format_name = str(args.get("format", "u32")).lower()
        fmt = "<I" if "32" in format_name else "<H"
        size = 4 if fmt == "<I" else 2
        count = _as_int(args.get("index_count"), 128)
        data_resp = await _dispatch_buffer(
            "get_data",
            {
                "session_id": args["session_id"],
                "buffer_id": args["index_buffer_id"],
                "offset": args.get("index_offset", 0),
                "size": count * size,
                "as_base64": True,
            },
        )
        payload = json.loads(data_resp)
        if not payload.get("success"):
            return data_resp
        import base64

        raw = base64.b64decode(payload.get("base64", ""))
        indices = []
        for i in range(0, len(raw), size):
            if i + size > len(raw):
                break
            indices.append(struct.unpack(fmt, raw[i : i + size])[0])
        return _ok(indices=indices)
    if action == "export":
        _require(args, "session_id", "event_id", "output_path")
        config_resp = await _dispatch_mesh("get_drawcall_mesh_config", {"session_id": args["session_id"], "event_id": args["event_id"]})
        payload = json.loads(config_resp)
        if not payload.get("success"):
            return config_resp
        out = Path(str(args["output_path"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload.get("mesh_config", {}), ensure_ascii=False, indent=2), encoding="utf-8")
        return _ok(saved_path=str(out))
    if action == "get_mesh_preview":
        _require(args, "session_id", "event_id")
        return await _dispatch_texture(
            "render_overlay",
            {
                "session_id": args["session_id"],
                "event_id": args["event_id"],
                "overlay": "wireframe",
                "output_path": args.get("output_path"),
                "file_format": "png",
            },
        )
    return _err(f"Unsupported mesh action: {action}")


async def _dispatch_texture(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])
    assert _render_service is not None

    async def read_npz(texture_id: Any, subresource: Optional[Dict[str, Any]], region: Optional[Dict[str, Any]]) -> Tuple[Any, Dict[str, Any], Optional[str]]:
        event_id = _active_event(session_id)
        if event_id <= 0:
            event_id = await _ensure_event(session_id, None)
        rid = await _resolve_texture_id(session_id, texture_id, event_id=event_id)
        artifact_ref, stats = await _render_service.readback_texture(
            session_id=session_id,
            event_id=event_id,
            texture_id=rid,
            session_manager=_session_manager,
            artifact_store=_artifact_store,
            subresource=subresource,
            region=region,
        )
        return artifact_ref, stats, _artifact_path(artifact_ref)

    if action in {"get_data", "get_subresource_data"}:
        _require(args, "texture_id")
        subresource = _as_dict(args.get("subresource"), default={})
        if action == "get_subresource_data":
            subresource = {
                "mip": _as_int(args.get("mip"), subresource.get("mip", 0)),
                "slice": _as_int(args.get("slice"), subresource.get("slice", 0)),
                "sample": _as_int(args.get("sample"), subresource.get("sample", 0)),
            }
        artifact_ref, stats, artifact_path = await read_npz(args.get("texture_id"), subresource, None)
        if artifact_path is None:
            return _err("Failed to store texture data artifact")
        result: Dict[str, Any] = {
            "artifact_path": artifact_path,
            "stats": stats,
            "byte_size": int(getattr(artifact_ref, "bytes", 0)),
        }
        if _as_bool(args.get("as_base64"), False):
            import base64

            payload = Path(artifact_path).read_bytes()
            result["base64"] = base64.b64encode(payload).decode("ascii")
        if args.get("output_path"):
            out = Path(str(args["output_path"]))
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact_path, out)
            result["saved_path"] = str(out)
        return _ok(**result)

    if action == "get_pixel_value":
        _require(args, "texture_id", "x", "y")
        event_id = await _ensure_event(session_id, None)
        rid = await _resolve_texture_id(session_id, args["texture_id"], event_id=event_id)
        pixel = await _render_service.pick_pixel(
            session_id=session_id,
            event_id=event_id,
            texture_id=rid,
            x=_as_int(args["x"]),
            y=_as_int(args["y"]),
            session_manager=_session_manager,
        )
        return _ok(pixel=pixel)

    if action == "get_region_values":
        _require(args, "texture_id", "rect")
        rect = _as_dict(args["rect"])
        region = {"x": _as_int(rect.get("x"), 0), "y": _as_int(rect.get("y"), 0), "width": _as_int(rect.get("w"), 1), "height": _as_int(rect.get("h"), 1)}
        subresource = {
            "mip": _as_int(args.get("mip"), 0),
            "slice": _as_int(args.get("slice"), 0),
            "sample": _as_int(args.get("sample"), 0),
        }
        artifact_ref, stats, artifact_path = await read_npz(args["texture_id"], subresource, region)
        return _ok(values_path=artifact_path, stats=stats)

    if action == "get_min_max":
        _require(args, "texture_id")
        event_id = await _ensure_event(session_id, None)
        rid = await _resolve_texture_id(session_id, args["texture_id"], event_id=event_id)
        stats = await _render_service.get_texture_stats(
            session_id=session_id,
            event_id=event_id,
            texture_id=rid,
            session_manager=_session_manager,
        )
        return _ok(min_max=stats)

    if action == "get_histogram":
        _require(args, "texture_id")
        try:
            import numpy as np
        except Exception as exc:
            return _err(f"numpy unavailable: {exc}")
        subresource = {"mip": _as_int(args.get("mip"), 0), "slice": _as_int(args.get("slice"), 0), "sample": 0}
        artifact_ref, stats, artifact_path = await read_npz(args["texture_id"], subresource, None)
        if artifact_path is None:
            return _err("Failed to generate histogram input")
        with np.load(artifact_path) as payload:
            arr = payload["pixels"]
        if arr.ndim < 3:
            return _ok(histogram={})
        channels = str(args.get("channels", "r")).lower()
        bins = _as_int(args.get("bins"), 256)
        rng = _as_dict(args.get("range"), default={})
        out: Dict[str, Any] = {}
        mapping = {"r": 0, "g": 1, "b": 2, "a": 3}
        for c in channels:
            if c not in mapping:
                continue
            channel_data = arr[..., mapping[c]].astype("float64")
            low = _as_float(rng.get("min"), float(channel_data.min()))
            high = _as_float(rng.get("max"), float(channel_data.max()))
            hist, edges = np.histogram(channel_data, bins=bins, range=(low, high))
            out[c] = {"bins": hist.tolist(), "edges": edges.tolist()}
        return _ok(histogram=out)

    if action == "get_pixel_history":
        _require(args, "texture_id", "x", "y")
        controller = await _get_controller(session_id)
        event_id = await _ensure_event(session_id, None)
        rid = await _resolve_texture_id(session_id, args["texture_id"], event_id=event_id)
        rd = _get_rd()
        sub = rd.Subresource()
        sub.mip = _as_int(args.get("mip"), 0)
        sub.slice = _as_int(args.get("slice"), 0)
        sub.sample = _as_int(args.get("sample"), 0)
        history_raw = None
        try:
            history_raw = await _offload(
                controller.PixelHistory,
                rid,
                _as_int(args["x"]),
                _as_int(args["y"]),
                sub,
                rd.CompType.Typeless,
            )
        except Exception:
            try:
                history_raw = await _offload(
                    controller.PixelHistory,
                    rid,
                    _as_int(args["x"]),
                    _as_int(args["y"]),
                    sub,
                )
            except Exception as exc:
                return _err(f"PixelHistory unavailable: {exc}")
        history = []
        for item in history_raw or []:
            history.append(
                {
                    "event_id": int(getattr(item, "eventId", 0)),
                    "primitive_id": int(getattr(item, "primitiveID", -1)),
                    "flags": str(getattr(item, "flags", "")),
                },
            )
        return _ok(history=history)

    if action == "render_overlay":
        event_id = _as_int(args.get("event_id"), _active_event(session_id))
        if event_id <= 0:
            event_id = await _ensure_event(session_id, None)
        explicit_texture_id = args.get("texture_id")
        if explicit_texture_id is not None and str(explicit_texture_id).strip():
            source_texture_id, texture_desc = await _get_texture_descriptor(
                session_id,
                explicit_texture_id,
                event_id=event_id,
            )
            source = {"source": "texture", "texture_id": source_texture_id}
        else:
            source_texture_id, texture_desc = await _get_texture_descriptor(
                session_id,
                None,
                event_id=event_id,
            )
            source = {"source": "final_output"}
        binding_index = await _binding_name_index_for_event(session_id, event_id)
        name_info = _compose_texture_name_info(
            source_texture_id,
            resource_name=str(getattr(texture_desc, "name", "")) if texture_desc is not None else "",
            binding_names=binding_index.get(str(source_texture_id), []),
            alias_name=_runtime.aliases.get(str(source_texture_id), ""),
        )
        channels_arg = _as_dict(args.get("channels"), default={})
        include_alpha = _as_bool(
            args.get("include_alpha"),
            _as_bool(channels_arg.get("a"), False),
        )
        view = {
            "overlay": str(args.get("overlay", "none")),
            "flip_y": _as_bool(args.get("flip_y"), False),
            "channels": {
                "r": _as_bool(channels_arg.get("r"), True),
                "g": _as_bool(channels_arg.get("g"), True),
                "b": _as_bool(channels_arg.get("b"), True),
                "a": include_alpha,
            },
        }
        artifact_ref, meta = await _render_service.render_event(
            session_id=session_id,
            event_id=event_id,
            session_manager=_session_manager,
            artifact_store=_artifact_store,
            source_config=source,
            view_config=view,
            output_format=str(args.get("file_format", "png")),
        )
        artifact_path = _artifact_path(artifact_ref)
        payload: Dict[str, Any] = {"artifact_path": artifact_path, "meta": meta}
        output_path = args.get("output_path")
        if output_path and artifact_path:
            out_path = Path(str(output_path))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact_path, out_path)
            payload["saved_path"] = str(out_path)
        payload["image_path"] = payload.get("saved_path") or artifact_path
        payload["name_info"] = name_info
        payload["texture_format"] = _texture_format_name(texture_desc)
        return _ok(**payload)

    if action == "save_to_file":
        event_id = _as_int(args.get("event_id"), _active_event(session_id))
        if event_id <= 0:
            event_id = await _ensure_event(session_id, None)
        texture_id, texture_desc = await _get_texture_descriptor(
            session_id,
            args.get("texture_id"),
            event_id=event_id,
        )
        binding_index = await _binding_name_index_for_event(session_id, event_id)
        binding_names = list(binding_index.get(str(texture_id), []))
        name_info = _compose_texture_name_info(
            texture_id,
            resource_name=str(getattr(texture_desc, "name", "")) if texture_desc is not None else "",
            binding_names=binding_names,
            alias_name=_runtime.aliases.get(str(texture_id), ""),
        )
        recommended_formats = _recommend_formats_for_texture(texture_desc, name_info=name_info, for_screenshot=False)
        requested_formats = _parse_requested_formats(args.get("file_format", "png"))
        selected_formats = _select_export_formats(
            requested_formats,
            recommended_formats=recommended_formats,
        )
        subresource = _as_dict(args.get("subresource"), default={})
        normalized_subresource = {
            "mip": _as_int(subresource.get("mip"), 0),
            "slice": _as_int(subresource.get("slice"), 0),
            "sample": _as_int(subresource.get("sample"), 0),
        }
        width = int(getattr(texture_desc, "width", 0)) if texture_desc is not None else 0
        height = int(getattr(texture_desc, "height", 0)) if texture_desc is not None else 0
        dim_token = f"_{width}x{height}" if width > 0 and height > 0 else ""
        base_name_stem = _safe_name_token(f"ev{event_id}_{name_info['name_stem']}{dim_token}")
        base_output_path = args.get("output_path")
        multi_export = len(selected_formats) > 1
        exports: List[Dict[str, Any]] = []
        for export_format in selected_formats:
            resolved_output_path = _resolve_export_output_path(
                base_output_path,
                name_stem=base_name_stem,
                file_format=export_format,
                multi=multi_export,
            )
            artifact_ref, meta, saved_path = await _render_service.save_texture_file(
                session_id=session_id,
                event_id=event_id,
                texture_id=texture_id,
                session_manager=_session_manager,
                artifact_store=_artifact_store,
                output_format=export_format,
                output_path=resolved_output_path,
                subresource=normalized_subresource,
            )
            artifact_path = _artifact_path(artifact_ref)
            exports.append(
                {
                    "file_format": export_format,
                    "artifact_path": artifact_path,
                    "saved_path": saved_path or artifact_path,
                    "meta": meta,
                },
            )
        if not multi_export:
            single = exports[0]
            return _ok(
                artifact_path=single["artifact_path"],
                saved_path=single["saved_path"],
                meta=single["meta"],
                selected_formats=selected_formats,
                requested_formats=requested_formats,
                recommended_formats=recommended_formats,
                name_info=name_info,
                texture_format=_texture_format_name(texture_desc),
            )
        return _ok(
            exports=exports,
            saved_paths=[item["saved_path"] for item in exports],
            selected_formats=selected_formats,
            requested_formats=requested_formats,
            recommended_formats=recommended_formats,
            name_info=name_info,
            texture_format=_texture_format_name(texture_desc),
        )

    if action == "save_mip_chain":
        _require(args, "texture_id", "output_dir")
        output_dir = Path(str(args["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        controller = await _get_controller(session_id)
        rid = await _resolve_texture_id(session_id, args["texture_id"], event_id=_active_event(session_id))
        textures = await _offload(controller.GetTextures)
        desc = next((t for t in textures if str(getattr(t, "resourceId", "")) in _resource_keys(rid)), None)
        mips = int(getattr(desc, "mips", 1)) if desc is not None else 1
        saved = []
        for mip in range(max(1, mips)):
            response = await _dispatch_texture(
                "save_to_file",
                {
                    "session_id": session_id,
                    "texture_id": args["texture_id"],
                    "subresource": {"mip": mip, "slice": _as_int(args.get("slice"), 0), "sample": 0},
                    "output_path": str(output_dir / f"mip_{mip:02d}.{str(args.get('file_format', 'png')).lower()}"),
                    "file_format": args.get("file_format", "png"),
                },
            )
            payload = json.loads(response)
            if payload.get("success"):
                saved.append(payload.get("saved_path"))
        return _ok(saved_paths=saved)

    if action in {"diff", "compute_stats"}:
        try:
            import numpy as np
        except Exception as exc:
            return _err(f"numpy unavailable: {exc}")
        if action == "diff":
            _require(args, "tex_a", "tex_b")
            tex_a = _as_dict(args["tex_a"])
            tex_b = _as_dict(args["tex_b"])
            art_a, _, path_a = await read_npz(tex_a.get("texture_id"), _as_dict(tex_a.get("subresource"), default={}), None)
            art_b, _, path_b = await read_npz(tex_b.get("texture_id"), _as_dict(tex_b.get("subresource"), default={}), None)
            if not path_a or not path_b:
                return _err("Could not read textures for diff")
            with np.load(path_a) as p1, np.load(path_b) as p2:
                arr_a = p1["pixels"].astype("float32")
                arr_b = p2["pixels"].astype("float32")
            min_h = min(arr_a.shape[0], arr_b.shape[0])
            min_w = min(arr_a.shape[1], arr_b.shape[1])
            arr_a = arr_a[:min_h, :min_w]
            arr_b = arr_b[:min_h, :min_w]
            diff = arr_a - arr_b
            mse = float((diff ** 2).mean()) if diff.size else 0.0
            max_abs = float(np.abs(diff).max()) if diff.size else 0.0
            psnr = float(20 * np.log10(1.0 / np.sqrt(mse))) if mse > 0 else float("inf")
            metrics = {"mse": mse, "max_abs": max_abs, "psnr": psnr}
            return _ok(diff=metrics)
        _require(args, "texture_id")
        _, stats, _ = await read_npz(args["texture_id"], {"mip": _as_int(args.get("mip"), 0), "slice": _as_int(args.get("slice"), 0), "sample": _as_int(args.get("sample"), 0)}, None)
        return _ok(stats=stats)

    return _err(f"Unsupported texture action: {action}")


async def _dispatch_shader(action: str, args: Dict[str, Any]) -> str:
    if action == "compile":
        return _err("On-host shader compilation is not configured")

    _require(args, "session_id")
    session_id = str(args["session_id"])
    controller = await _get_controller(session_id)
    event_id = await _ensure_event(session_id, args.get("event_id"))
    pipe = await _offload(controller.GetPipelineState)

    async def _find_stage_by_shader(shader_id: str) -> Optional[str]:
        for stage in _stage_candidates():
            rd_stage = _rd_stage(stage)
            bound = await _offload(pipe.GetShader, rd_stage)
            if shader_id in _resource_keys(bound):
                return stage
        return None

    if action == "debug_start":
        _require(args, "params")
        mode = str(args.get("mode", "pixel")).lower()
        params = _as_dict(args.get("params"))
        if mode != "pixel":
            return _err("Only pixel debug mode is currently supported")
        rd = _get_rd()
        trace = await _offload(
            controller.DebugPixel,
            _as_int(params.get("x"), 0),
            _as_int(params.get("y"), 0),
            rd.DebugPixelInputs(),
        )
        if trace is None or not getattr(trace, "valid", False):
            return _err("DebugPixel returned invalid trace")
        shader_debug_id = _new_id("sdbg")
        _runtime.shader_debugs[shader_debug_id] = ShaderDebugHandle(
            shader_debug_id=shader_debug_id,
            session_id=session_id,
            mode=mode,
            event_id=event_id,
            trace=trace,
            debugger=getattr(trace, "debugger", None),
            current_state=None,
        )
        return _ok(shader_debug_id=shader_debug_id, initial_state={"pc": 0})

    if action in {"get_debug_state", "list_replacements", "revert_replacement", "edit_and_replace", "get_messages", "save_binary", "extract_binary", "get_source", "list_entry_points", "get_bindpoint_mapping", "get_constant_block_layout", "get_constant_buffer_contents", "get_reflection", "get_disassembly"}:
        pass
    else:
        return _err(f"Unsupported shader action: {action}")

    if action == "get_debug_state":
        debug_id = str(args.get("shader_debug_id", ""))
        if not debug_id:
            return _err("Missing required parameter(s): shader_debug_id")
        handle = _runtime.shader_debugs.get(debug_id)
        if handle is None:
            return _err(f"Unknown shader_debug_id: {debug_id}")
        state = handle.current_state
        payload = {"pc": int(getattr(state, "stepIndex", 0)) if state is not None else 0}
        return _ok(state=payload)

    if action == "list_replacements":
        replacements = _runtime.shader_replacements.get(session_id, [])
        return _ok(replacements=replacements)

    if action == "revert_replacement":
        _require(args, "replacement_id")
        replacement_id = str(args["replacement_id"])
        repl = _runtime.shader_replacements.get(session_id, [])
        _runtime.shader_replacements[session_id] = [r for r in repl if str(r.get("replacement_id")) != replacement_id]
        return _ok()

    if action == "edit_and_replace":
        stage = _parse_stage(args.get("stage"))
        replacement = {
            "replacement_id": _new_id("repl"),
            "stage": stage.upper(),
            "original_shader_id": str(args.get("shader_id", "")),
            "status": "mock_applied",
            "messages": ["Runtime replacement API is not exposed in this build; recorded as logical replacement only."],
        }
        _runtime.shader_replacements.setdefault(session_id, []).append(replacement)
        return _ok(replacement_id=replacement["replacement_id"], status=replacement["status"], messages=replacement["messages"])

    if action == "get_messages":
        severity_min = str(args.get("severity_min", "info"))
        replacements = _runtime.shader_replacements.get(session_id, [])
        messages = []
        for r in replacements:
            for msg in r.get("messages", []):
                messages.append({"severity": "info", "message": msg})
        return _ok(messages=messages, severity_min=severity_min)

    if action == "save_binary":
        return _err("Shader binary extraction is not available via this replay backend")
    if action == "extract_binary":
        return _err("Shader binary extraction is not available via this replay backend")
    if action == "get_source":
        return _ok(source=None, files=[])

    if action == "get_constant_buffer_contents":
        stage_name = _parse_stage(args.get("stage"))
        rd_stage_cb = _rd_stage(stage_name)
        constant_blocks = await _offload(pipe.GetConstantBlocks, rd_stage_cb)
        out = []
        for cb in constant_blocks or []:
            bind = getattr(cb, "bindPoint", None)
            for buf in (getattr(cb, "buffers", None) or []):
                out.append(
                    {
                        "slot": int(getattr(bind, "bind", 0)) if bind is not None else 0,
                        "resource_id": str(getattr(buf, "resourceId", "")),
                        "offset": int(getattr(buf, "byteOffset", 0)),
                        "size": int(getattr(buf, "byteSize", 0)),
                    },
                )
        return _ok(cbuffer={"vars": out})

    if action in {"list_entry_points", "get_bindpoint_mapping", "get_constant_block_layout", "get_reflection", "get_disassembly"}:
        _require(args, "shader_id")
        shader_id = str(args["shader_id"])
        stage = await _find_stage_by_shader(shader_id)
        if stage is None:
            return _err(f"Shader not bound at current event: {shader_id}")
        rd_stage = _rd_stage(stage)
        reflection = await _offload(pipe.GetShaderReflection, rd_stage)
        if action == "list_entry_points":
            entries = []
            if reflection is not None:
                entries.append({"name": str(getattr(reflection, "entryPoint", "main")), "stage": stage.upper()})
            return _ok(entry_points=entries)
        if action == "get_bindpoint_mapping":
            mapping = []
            if reflection is not None:
                for ro in getattr(reflection, "readOnlyResources", []) or []:
                    mapping.append({"resource_name": str(getattr(ro, "name", "")), "bindpoint": int(getattr(ro, "bindPoint", 0)), "type": "SRV", "stage": stage.upper()})
                for rw in getattr(reflection, "readWriteResources", []) or []:
                    mapping.append({"resource_name": str(getattr(rw, "name", "")), "bindpoint": int(getattr(rw, "bindPoint", 0)), "type": "UAV", "stage": stage.upper()})
            return _ok(mapping=mapping)
        if action == "get_constant_block_layout":
            block_name_or_index = args.get("block_name_or_index")
            blocks = getattr(reflection, "constantBlocks", []) or []
            selected = None
            if isinstance(block_name_or_index, int):
                if 0 <= block_name_or_index < len(blocks):
                    selected = blocks[block_name_or_index]
            else:
                key = str(block_name_or_index)
                for block in blocks:
                    if str(getattr(block, "name", "")) == key:
                        selected = block
                        break
            if selected is None and blocks:
                selected = blocks[0]
            if selected is None:
                return _ok(layout={})
            layout = {
                "name": str(getattr(selected, "name", "")),
                "byte_size": int(getattr(selected, "byteSize", 0)),
                "vars": [
                    {
                        "name": str(getattr(v, "name", "")),
                        "offset": int(getattr(v, "byteOffset", 0)),
                        "type": str(getattr(getattr(v, "type", None), "descriptor", None)),
                    }
                    for v in (getattr(selected, "variables", []) or [])
                ],
            }
            return _ok(layout=layout)
        if action == "get_reflection":
            refl = {
                "entry_points": [str(getattr(reflection, "entryPoint", "main"))] if reflection else [],
                "inputs": [],
                "outputs": [],
                "resources": [],
                "constant_blocks": [],
                "samplers": [],
            }
            if reflection is not None:
                for ro in getattr(reflection, "readOnlyResources", []) or []:
                    refl["resources"].append({"name": str(getattr(ro, "name", "")), "bindpoint": int(getattr(ro, "bindPoint", 0)), "type": "SRV"})
                for cb in getattr(reflection, "constantBlocks", []) or []:
                    refl["constant_blocks"].append({"name": str(getattr(cb, "name", "")), "byte_size": int(getattr(cb, "byteSize", 0))})
            return _ok(reflection=refl)
        if action == "get_disassembly":
            targets = await _offload(controller.GetDisassemblyTargets, True)
            target = str(args.get("target", "auto"))
            if target == "auto":
                target = targets[0] if targets else ""
            if not target:
                return _ok(disassembly="", target="")
            pipeline_obj = await _offload(pipe.GetComputePipelineObject) if stage == "cs" else await _offload(pipe.GetGraphicsPipelineObject)
            text = await _offload(controller.DisassembleShader, pipeline_obj, reflection, target)
            return _ok(disassembly=str(text), target=target)

    return _err(f"Unsupported shader action: {action}")


async def _dispatch_debug(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])

    if action == "pixel_history":
        target = _parse_target_like(args.get("target"))
        texture_id = target.get("texture_id") or target.get("textureId")
        if texture_id is None:
            texture_id = await _resolve_texture_id(session_id, None, event_id=_active_event(session_id))
        return await _dispatch_texture(
            "get_pixel_history",
            {
                "session_id": session_id,
                "texture_id": str(texture_id),
                "x": _as_int(args.get("x"), 0),
                "y": _as_int(args.get("y"), 0),
                "sample": _as_int(args.get("sample"), 0),
            },
        )

    if action == "explain_test_failure":
        _require(args, "history_item")
        item = _as_dict(args["history_item"])
        reason = str(item.get("flags", "unknown"))
        explanation = f"Pixel test outcome is flagged as '{reason}'. Review depth/stencil/blend state around event {item.get('event_id')}."
        return _ok(explanation=explanation, key_facts={"event_id": item.get("event_id"), "flags": reason})

    _require(args, "shader_debug_id")
    debug_id = str(args["shader_debug_id"])
    handle = _runtime.shader_debugs.get(debug_id)
    if handle is None:
        return _err(f"Unknown shader_debug_id: {debug_id}")
    if handle.session_id != session_id:
        return _err("shader_debug_id does not belong to session_id")
    controller = await _get_controller(session_id)

    async def continue_once() -> Optional[Any]:
        states = await _offload(controller.ContinueDebug, handle.debugger)
        if not states:
            return None
        handle.current_state = states[-1]
        return handle.current_state

    if action == "step":
        state = await continue_once()
        if state is None:
            handle.stopped_reason = "finished"
            return _ok(state={}, stopped_reason="finished")
        return _ok(state={"pc": int(getattr(state, "stepIndex", 0))})
    if action == "continue":
        timeout_ms = _as_int(args.get("timeout_ms"), 10000)
        deadline = _now_ms() + timeout_ms
        state = None
        while _now_ms() < deadline:
            state = await continue_once()
            if state is None:
                handle.stopped_reason = "finished"
                return _ok(state={}, stopped_reason="finished")
            if handle.breakpoints:
                pc = int(getattr(state, "stepIndex", 0))
                if any(bp.get("pc") == pc for bp in handle.breakpoints):
                    handle.stopped_reason = "breakpoint"
                    return _ok(state={"pc": pc}, stopped_reason="breakpoint")
        handle.stopped_reason = "timeout"
        return _ok(state={"pc": int(getattr(state, "stepIndex", 0)) if state is not None else 0}, stopped_reason="timeout")
    if action == "run_to":
        target = _as_dict(args.get("target"), default={})
        target_pc = target.get("pc")
        if target_pc is None:
            return _err("run_to currently supports target.pc only")
        timeout_ms = _as_int(args.get("timeout_ms"), 10000)
        deadline = _now_ms() + timeout_ms
        while _now_ms() < deadline:
            state = await continue_once()
            if state is None:
                handle.stopped_reason = "finished"
                return _ok(state={}, stopped_reason="finished")
            pc = int(getattr(state, "stepIndex", 0))
            if pc == int(target_pc):
                return _ok(state={"pc": pc})
        return _err("run_to timeout")
    if action == "set_breakpoints":
        bps = _as_list(args.get("breakpoints"), default=[])
        handle.breakpoints = [_as_dict(bp) for bp in bps]
        return _ok(active_breakpoints=handle.breakpoints)
    if action == "clear_breakpoints":
        handle.breakpoints = []
        return _ok()
    if action == "get_variables":
        state = handle.current_state
        if state is None:
            return _ok(variables=[])
        changes = getattr(state, "changes", None) or []
        variables = []
        for change in changes:
            name = str(getattr(change, "name", ""))
            if not name:
                continue
            value = getattr(change, "value", None)
            variables.append({"name": name, "type": str(type(value).__name__), "value": str(value)})
        return _ok(variables=variables[: _as_int(args.get("max_variables"), 2048)])
    if action == "evaluate_expression":
        _require(args, "expression")
        expr = str(args["expression"])
        vars_payload_resp = await _dispatch_debug("get_variables", {"session_id": session_id, "shader_debug_id": debug_id})
        vars_payload = json.loads(vars_payload_resp)
        values: Dict[str, Any] = {}
        for var in vars_payload.get("variables", []):
            values[var["name"]] = var.get("value")
        if expr in values:
            return _ok(value=values[expr])
        return _err(f"Unknown expression or variable: {expr}")
    if action == "get_callstack":
        state = handle.current_state
        callstack = []
        if state is not None:
            raw = getattr(state, "callstack", None) or []
            for frame in raw:
                callstack.append(
                    {
                        "function": str(getattr(frame, "function", "")),
                        "file": str(getattr(frame, "file", "")),
                        "line": int(getattr(frame, "line", 0)),
                        "pc": str(getattr(frame, "address", "")),
                    },
                )
        return _ok(callstack=callstack)
    if action == "finish":
        try:
            await _offload(controller.FreeTrace, handle.trace)
        except Exception:
            pass
        _runtime.shader_debugs.pop(debug_id, None)
        return _ok()

    return _err(f"Unsupported debug action: {action}")


async def _dispatch_perf(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])
    if action == "enumerate_counters":
        counters = await _perf_service.enumerate_counters(session_id, _session_manager)
        return _ok(counters=counters)
    if action == "describe_counter":
        _require(args, "counter_id")
        counters = await _perf_service.enumerate_counters(session_id, _session_manager)
        cid = _as_int(args["counter_id"])
        for counter in counters:
            if int(counter.get("counter_id", -1)) == cid:
                return _ok(counter=counter)
        return _err(f"Counter not found: {cid}")
    if action == "sample_counters":
        counter_ids = [int(v) for v in _as_list(args.get("counter_ids"), default=[])]
        event_range = _as_dict(args.get("event_range"), default={})
        lo = _as_int(event_range.get("start_event_id", event_range.get("lo", 0)), 0)
        hi_default = _active_event(session_id) or 10**9
        hi = _as_int(event_range.get("end_event_id", event_range.get("hi", hi_default)), hi_default)
        if not counter_ids:
            all_counters = await _perf_service.enumerate_counters(session_id, _session_manager)
            counter_ids = [int(c["counter_id"]) for c in all_counters]
        perf = await _perf_service.sample_counters(session_id, (lo, hi), counter_ids, _session_manager)
        return _ok(perf=perf.model_dump(mode="json"))
    if action == "get_event_durations":
        top = await _perf_service.detect_hotspots(session_id, _session_manager, top_k=_as_int(args.get("max_events"), 200))
        return _ok(event_durations=top)
    if action == "get_frame_timing":
        hotspots = await _perf_service.detect_hotspots(session_id, _session_manager, top_k=20)
        total = sum(float(x.get("duration_us", 0.0)) for x in hotspots)
        return _ok(frame_timing={"gpu_duration_us_sum_top20": total, "hotspots": hotspots})
    if action == "get_pipeline_statistics":
        stats = {"event_id": _as_int(args.get("event_id"), _active_event(session_id)), "draws": 0, "dispatches": 0}
        return _ok(pipeline_statistics=stats)
    return _err(f"Unsupported perf action: {action}")


async def _dispatch_export(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])

    if action == "screenshot":
        target = _parse_target_like(args.get("target"))
        event_id = _as_int(args.get("event_id"), _active_event(session_id))
        if event_id <= 0:
            event_id = await _ensure_event(session_id, None)
        explicit_target = target.get("texture_id") or target.get("textureId")
        chosen_output_slot: Optional[int] = None
        if explicit_target:
            target_texture_id, texture_desc = await _get_texture_descriptor(
                session_id,
                explicit_target,
                event_id=event_id,
            )
        else:
            output_targets = await _output_target_resource_ids(session_id, event_id)
            best_texture_id: Optional[Any] = None
            best_score = float("-inf")
            for candidate_id, candidate_slot in output_targets:
                try:
                    stats = await _render_service.get_texture_stats(
                        session_id=session_id,
                        event_id=event_id,
                        texture_id=candidate_id,
                        session_manager=_session_manager,
                    )
                    channels = _as_dict(stats.get("channels"), default={})
                    rgb_spread = 0.0
                    for channel in ("r", "g", "b"):
                        cd = _as_dict(channels.get(channel), default={})
                        cmin = cd.get("min")
                        cmax = cd.get("max")
                        if cmin is None or cmax is None:
                            continue
                        try:
                            rgb_spread += abs(float(cmax) - float(cmin))
                        except Exception:
                            continue
                    alpha = _as_dict(channels.get("a"), default={})
                    try:
                        alpha_spread = abs(float(alpha.get("max", 0.0)) - float(alpha.get("min", 0.0)))
                    except Exception:
                        alpha_spread = 0.0
                    score = (rgb_spread * 10.0) + alpha_spread
                    if not _as_bool(stats.get("has_any_nan"), False) and not _as_bool(stats.get("has_any_inf"), False):
                        score += 0.1
                    if score > best_score:
                        best_score = score
                        best_texture_id = candidate_id
                        chosen_output_slot = candidate_slot
                except Exception:
                    continue

            if best_texture_id is None:
                if output_targets:
                    best_texture_id, chosen_output_slot = output_targets[0]
                else:
                    best_texture_id, _ = await _get_texture_descriptor(
                        session_id,
                        None,
                        event_id=event_id,
                    )
            target_texture_id, texture_desc = await _get_texture_descriptor(
                session_id,
                best_texture_id,
                event_id=event_id,
            )
        binding_index = await _binding_name_index_for_event(session_id, event_id)
        name_info = _compose_texture_name_info(
            target_texture_id,
            resource_name=str(getattr(texture_desc, "name", "")) if texture_desc is not None else "",
            binding_names=binding_index.get(str(target_texture_id), []),
            alias_name=_runtime.aliases.get(str(target_texture_id), ""),
        )
        recommended_formats = _recommend_formats_for_texture(
            texture_desc,
            name_info=name_info,
            for_screenshot=True,
        )
        requested_formats = _parse_requested_formats(args.get("file_format", "png"))
        selected_formats = _select_export_formats(
            requested_formats,
            recommended_formats=recommended_formats,
        )
        allowed_formats = {"png", "jpg", "exr", "hdr"}
        valid_formats = [fmt for fmt in selected_formats if fmt in allowed_formats]
        if not valid_formats:
            allowed_text = ", ".join(sorted(allowed_formats))
            requested_text = ", ".join(selected_formats) or ", ".join(requested_formats)
            return _err(
                f"rd.export.screenshot only supports {allowed_text}; got '{requested_text}'",
            )
        base_output_path = args.get("output_path")
        if chosen_output_slot is not None and not explicit_target:
            role_stem = f"framebuffer_rt{chosen_output_slot}"
        else:
            role_stem = "framebuffer"
        base_name_stem = _safe_name_token(f"ev{event_id}_{role_stem}_{name_info['name_stem']}")
        multi_export = len(valid_formats) > 1
        include_alpha = _as_bool(args.get("include_alpha"), False)
        exports: List[Dict[str, Any]] = []
        for export_format in valid_formats:
            resolved_output_path = _resolve_export_output_path(
                base_output_path,
                name_stem=base_name_stem,
                file_format=export_format,
                multi=multi_export,
            )
            response = await _dispatch_texture(
                "render_overlay",
                {
                    "session_id": session_id,
                    "texture_id": str(target_texture_id),
                    "event_id": event_id,
                    "overlay": args.get("overlay", "none"),
                    "output_path": resolved_output_path,
                    "file_format": export_format,
                    "include_alpha": include_alpha,
                },
            )
            payload = json.loads(response)
            if not payload.get("success"):
                return response
            exports.append(
                {
                    "file_format": export_format,
                    "artifact_path": payload.get("artifact_path"),
                    "saved_path": payload.get("saved_path") or payload.get("image_path") or payload.get("artifact_path"),
                    "image_path": payload.get("image_path") or payload.get("saved_path") or payload.get("artifact_path"),
                    "meta": payload.get("meta"),
                },
            )
        if not multi_export:
            single = exports[0]
            return _ok(
                artifact_path=single["artifact_path"],
                saved_path=single["saved_path"],
                image_path=single["image_path"],
                meta=single["meta"],
                selected_formats=valid_formats,
                requested_formats=requested_formats,
                recommended_formats=recommended_formats,
                name_info=name_info,
                texture_format=_texture_format_name(texture_desc),
                chosen_output_slot=chosen_output_slot,
            )
        return _ok(
            exports=exports,
            saved_paths=[item["saved_path"] for item in exports],
            image_paths=[item["image_path"] for item in exports],
            selected_formats=valid_formats,
            requested_formats=requested_formats,
            recommended_formats=recommended_formats,
            name_info=name_info,
            texture_format=_texture_format_name(texture_desc),
            chosen_output_slot=chosen_output_slot,
        )
    if action == "texture":
        return await _dispatch_texture(
            "save_to_file",
            {
                "session_id": session_id,
                "texture_id": args.get("texture_id"),
                "event_id": args.get("event_id"),
                "subresource": args.get("subresource"),
                "output_path": args.get("output_path"),
                "file_format": args.get("file_format", "png"),
            },
        )
    if action == "buffer":
        return await _dispatch_buffer(
            "save_to_file",
            {
                "session_id": session_id,
                "buffer_id": args.get("buffer_id"),
                "offset": args.get("offset"),
                "size": args.get("size"),
                "output_path": args.get("output_path"),
            },
        )
    if action == "mesh":
        return await _dispatch_mesh(
            "export",
            {
                "session_id": session_id,
                "event_id": args.get("event_id"),
                "format": args.get("format"),
                "output_path": args.get("output_path"),
            },
        )
    if action == "pipeline_state_json":
        _require(args, "output_path")
        state_resp = await _dispatch_pipeline("get_state", {"session_id": session_id, "detail_level": args.get("detail_level", "full")})
        payload = json.loads(state_resp)
        if not payload.get("success"):
            return state_resp
        out = Path(str(args["output_path"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload.get("pipeline_state", {}), ensure_ascii=False, indent=2), encoding="utf-8")
        return _ok(saved_path=str(out))
    if action == "event_tree_json":
        _require(args, "output_path")
        event_resp = await _dispatch_event("get_action_tree", {"session_id": session_id, "max_depth": args.get("max_depth")})
        payload = json.loads(event_resp)
        if not payload.get("success"):
            return event_resp
        out = Path(str(args["output_path"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload.get("root", {}), ensure_ascii=False, indent=2), encoding="utf-8")
        return _ok(saved_path=str(out))
    if action == "resource_list_csv":
        _require(args, "output_path")
        resources_resp = await _dispatch_resource("list_all", {"session_id": session_id})
        payload = json.loads(resources_resp)
        if not payload.get("success"):
            return resources_resp
        rows = payload.get("resources", [])
        out = Path(str(args["output_path"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=sorted({k for row in rows for k in row.keys()}))
            writer.writeheader()
            writer.writerows(rows)
        return _ok(saved_path=str(out))
    if action == "pixel_history_json":
        _require(args, "output_path")
        debug_resp = await _dispatch_debug(
            "pixel_history",
            {
                "session_id": session_id,
                "x": args.get("x"),
                "y": args.get("y"),
                "target": args.get("target"),
            },
        )
        payload = json.loads(debug_resp)
        if not payload.get("success"):
            return debug_resp
        out = Path(str(args["output_path"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload.get("history", []), ensure_ascii=False, indent=2), encoding="utf-8")
        return _ok(saved_path=str(out))
    if action == "shader_bundle":
        _require(args, "event_id", "output_dir")
        output_dir = Path(str(args["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        stage_payloads = []
        for stage in _stage_candidates():
            shader_resp = await _dispatch_pipeline("get_shader", {"session_id": session_id, "stage": stage})
            shader_payload = json.loads(shader_resp)
            if shader_payload.get("success") and shader_payload.get("shader", {}).get("shader_id"):
                stage_payloads.append(shader_payload["shader"])
        bundle_json = output_dir / "shader_bundle.json"
        bundle_json.write_text(json.dumps(stage_payloads, ensure_ascii=False, indent=2), encoding="utf-8")
        return _ok(output_dir=str(output_dir), bundle_path=str(bundle_json))
    if action == "cbuffer_dump":
        _require(args, "output_dir")
        output_dir = Path(str(args["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        stages = _as_list(args.get("stages"), default=["vs", "ps", "cs"])
        dumped = []
        for stage in stages:
            resp = await _dispatch_shader(
                "get_constant_buffer_contents",
                {"session_id": session_id, "shader_id": args.get("shader_id"), "stage": stage},
            )
            payload = json.loads(resp)
            if payload.get("success"):
                file_path = output_dir / f"cbuffer_{stage}.json"
                file_path.write_text(json.dumps(payload.get("cbuffer", {}), ensure_ascii=False, indent=2), encoding="utf-8")
                dumped.append(str(file_path))
        return _ok(dumped_paths=dumped)
    if action == "repro_bundle_zip":
        _require(args, "output_path")
        output_path = Path(str(args["output_path"]))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bundle/manifest.json", json.dumps({"session_id": session_id, "created_at": _now_ms()}, ensure_ascii=False, indent=2))
        return _ok(saved_path=str(output_path))
    if action == "markdown_report":
        _require(args, "output_path")
        summary_resp = await _dispatch_macro("summarize_frame", {"session_id": session_id})
        payload = json.loads(summary_resp)
        if not payload.get("success"):
            return summary_resp
        report = textwrap.dedent(
            f"""\
            # RenderDoc MCP Report
            - Session: `{session_id}`
            - Created: `{datetime.now(timezone.utc).isoformat()}`

            ## Frame Summary
            ```json
            {json.dumps(payload.get("summary", {}), ensure_ascii=False, indent=2)}
            ```
            """,
        )
        out = Path(str(args["output_path"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        return _ok(saved_path=str(out))
    return _err(f"Unsupported export action: {action}")


async def _dispatch_diag(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])
    state_resp = await _dispatch_pipeline("get_state_summary", {"session_id": session_id})
    state_payload = json.loads(state_resp)
    if not state_payload.get("success"):
        return state_resp
    summary = state_payload.get("summary", {})
    issues: List[Dict[str, Any]] = []

    if action == "scan_common_issues":
        rt_count = len(summary.get("render_targets", []))
        if rt_count == 0:
            issues.append({"severity": "error", "check": "render_targets", "message": "No render targets bound."})
        if not summary.get("shaders"):
            issues.append({"severity": "warn", "check": "shaders", "message": "No shader is bound at active event."})
        if not summary.get("topology"):
            issues.append({"severity": "warn", "check": "topology", "message": "Primitive topology is empty."})
        return _ok(issues=issues, suggestions=["Check active event", "Verify drawcall context"] if _as_bool(args.get("include_suggestions"), True) else [])

    if action == "check_render_targets":
        return _ok(report={"render_target_count": len(summary.get("render_targets", [])), "issues": issues})
    if action == "check_depth_stencil":
        depth_resp = await _dispatch_pipeline("get_depth_stencil_state", {"session_id": session_id})
        payload = json.loads(depth_resp)
        return _ok(report={"depth_stencil": payload.get("depth_stencil", {}), "issues": issues})
    if action == "check_viewport_scissor":
        vs_resp = await _dispatch_pipeline("get_viewports_scissors", {"session_id": session_id})
        payload = json.loads(vs_resp)
        return _ok(report={"viewports": payload.get("viewports", []), "scissors": payload.get("scissors", []), "issues": issues})
    if action == "check_culling":
        raster_resp = await _dispatch_pipeline("get_rasterizer_state", {"session_id": session_id})
        payload = json.loads(raster_resp)
        return _ok(report={"rasterizer": payload.get("rasterizer", {}), "issues": issues})
    if action == "check_blend":
        blend_resp = await _dispatch_pipeline("get_blend_state", {"session_id": session_id})
        payload = json.loads(blend_resp)
        return _ok(report={"blend": payload.get("blend", {}), "issues": issues})
    if action == "check_srgb":
        rts = summary.get("render_targets", [])
        srgb_count = sum(1 for rt in rts if str(rt.get("format", "")).lower().find("srgb") >= 0)
        return _ok(report={"srgb_target_count": srgb_count, "issues": issues})
    if action == "check_resource_bindings":
        bind_resp = await _dispatch_pipeline("get_resource_bindings", {"session_id": session_id})
        payload = json.loads(bind_resp)
        return _ok(report={"binding_count": len(payload.get("bindings", [])), "issues": issues})
    if action == "check_constant_buffers":
        cb_resp = await _dispatch_pipeline("get_constant_buffers", {"session_id": session_id})
        payload = json.loads(cb_resp)
        return _ok(report={"constant_buffers": payload.get("constant_buffers", []), "issues": issues})
    if action == "check_d3d12_resource_states":
        return _ok(report={"supported": False, "issues": [{"severity": "info", "message": "Detailed D3D12 state tracking is unavailable in this backend."}]})
    if action == "check_vk_dynamic_state":
        return _ok(report={"supported": False, "issues": [{"severity": "info", "message": "Detailed Vulkan dynamic state tracking is unavailable in this backend."}]})

    return _err(f"Unsupported diag action: {action}")


async def _dispatch_macro(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])

    if action == "summarize_frame":
        event_resp = await _dispatch_event("get_actions", {"session_id": session_id, "include_markers": True, "include_drawcalls": True})
        pipeline_resp = await _dispatch_pipeline("get_state_summary", {"session_id": session_id})
        event_payload = json.loads(event_resp)
        pipeline_payload = json.loads(pipeline_resp)
        if not event_payload.get("success"):
            return event_resp
        if not pipeline_payload.get("success"):
            return pipeline_resp
        actions = event_payload.get("actions", [])
        flat = []

        def walk(nodes: List[Dict[str, Any]]) -> None:
            for node in nodes:
                flat.append(node)
                walk(node.get("children", []))

        walk(actions)
        summary = {
            "event_count": len(flat),
            "draw_count": sum(1 for n in flat if n.get("flags", {}).get("is_draw")),
            "marker_count": sum(1 for n in flat if n.get("flags", {}).get("is_marker")),
            "pipeline": pipeline_payload.get("summary", {}),
        }
        return _ok(summary=summary)

    if action == "generate_pass_summary":
        _require(args, "pass_range")
        pass_range = _as_dict(args["pass_range"])
        start_evt = _as_int(pass_range.get("begin_event_id"), 0)
        end_evt = _as_int(pass_range.get("end_event_id"), 0)
        return _ok(summary={"begin_event_id": start_evt, "end_event_id": end_evt, "drawcall_count": max(0, end_evt - start_evt + 1)})

    if action == "find_pass_by_marker":
        _require(args, "name_regex")
        regex_text = str(args["name_regex"])
        flags = re.IGNORECASE if _as_bool(args.get("ignore_case"), False) else 0
        try:
            pattern = re.compile(regex_text, flags)
        except re.error as exc:
            return _err(f"Invalid regex: {exc}")
        max_results = _as_int(args.get("max_results"), 20)
        event_resp = await _dispatch_event(
            "get_actions",
            {"session_id": session_id, "include_markers": True, "include_drawcalls": True},
        )
        payload = json.loads(event_resp)
        if not payload.get("success"):
            return event_resp
        roots = payload.get("actions", [])
        matches: List[Dict[str, Any]] = []

        def walk(nodes: Sequence[Any], path_stack: List[str]) -> None:
            if len(matches) >= max_results:
                return
            for node in nodes:
                if len(matches) >= max_results:
                    return
                if not isinstance(node, dict):
                    continue
                name = str(node.get("name", ""))
                next_path = path_stack + ([name] if name else [])
                haystacks = [name, " > ".join(next_path)]
                if any(pattern.search(h) for h in haystacks):
                    matches.append(
                        {
                            "event_id": int(node.get("event_id", 0)),
                            "name": name,
                            "flags": node.get("flags", {}),
                            "path": next_path,
                        },
                    )
                    if len(matches) >= max_results:
                        return
                children = node.get("children", [])
                if isinstance(children, list):
                    walk(children, next_path)

        walk(roots if isinstance(roots, list) else [], [])
        return _ok(matches=matches)

    if action == "locate_draw_affecting_pixel":
        _require(args, "x", "y")
        history_resp = await _dispatch_debug("pixel_history", {"session_id": session_id, "x": args["x"], "y": args["y"], "target": args.get("target")})
        payload = json.loads(history_resp)
        if not payload.get("success"):
            return history_resp
        candidates = payload.get("history", [])
        return _ok(candidates=candidates)

    if action == "explain_pixel":
        _require(args, "x", "y")
        history_resp = await _dispatch_debug("pixel_history", {"session_id": session_id, "x": args["x"], "y": args["y"], "target": args.get("target")})
        payload = json.loads(history_resp)
        if not payload.get("success"):
            return history_resp
        history = payload.get("history", [])
        explanation = f"Pixel({args['x']},{args['y']}) has {len(history)} recorded modifications in pixel history."
        return _ok(explanation=explanation, history=history[:20])

    if action == "trace_resource_lifetime":
        _require(args, "resource_id")
        return await _dispatch_resource(
            "get_history",
            {
                "session_id": session_id,
                "resource_id": args["resource_id"],
                "include_reads": args.get("include_reads", False),
                "include_writes": True,
            },
        )

    if action == "resource_dependency_graph":
        event_tree_resp = await _dispatch_event("get_action_tree", {"session_id": session_id, "max_depth": 4})
        payload = json.loads(event_tree_resp)
        if not payload.get("success"):
            return event_tree_resp
        graph = {"nodes": [], "edges": []}
        root = payload.get("root", {})
        queue = [root]
        while queue:
            node = queue.pop(0)
            event_id = node.get("event_id")
            if event_id is not None:
                graph["nodes"].append({"id": f"evt_{event_id}", "label": node.get("name", "")})
            for child in node.get("children", []):
                cid = child.get("event_id")
                if event_id is not None and cid is not None:
                    graph["edges"].append({"from": f"evt_{event_id}", "to": f"evt_{cid}"})
                queue.append(child)
        return _ok(graph=graph)

    if action == "find_state_change_point":
        _require(args, "event_range", "state_path", "target_value")
        event_range = _as_dict(args["event_range"])
        start_evt = _as_int(event_range.get("start_event_id"), 0)
        end_evt = _as_int(event_range.get("end_event_id"), 0)
        path = str(args["state_path"])
        target_value = args["target_value"]
        found = None
        for evt in range(start_evt, end_evt + 1):
            snapshot = await _pipeline_service.snapshot_pipeline(session_id, evt, _session_manager)
            payload = snapshot.model_dump(mode="json")
            cursor: Any = payload
            ok = True
            for token in path.split("."):
                if isinstance(cursor, dict) and token in cursor:
                    cursor = cursor[token]
                else:
                    ok = False
                    break
            if ok and cursor == target_value:
                found = evt
                break
        return _ok(found_event_id=found)

    if action == "compare_events_report":
        _require(args, "event_a", "event_b")
        diff_resp = await _dispatch_event("diff_pipeline_state", {"session_id": session_id, "event_a": args["event_a"], "event_b": args["event_b"]})
        payload = json.loads(diff_resp)
        if not payload.get("success"):
            return diff_resp
        if args.get("output_path"):
            out = Path(str(args["output_path"]))
            out.parent.mkdir(parents=True, exist_ok=True)
            lines = ["# Event Comparison Report", f"- Event A: {args['event_a']}", f"- Event B: {args['event_b']}", "", "## Differences"]
            for item in payload.get("diff", [])[:200]:
                lines.append(f"- `{item.get('path')}`: `{item.get('before')}` -> `{item.get('after')}`")
            out.write_text("\n".join(lines), encoding="utf-8")
            return _ok(saved_path=str(out), diff=payload.get("diff", []))
        return _ok(diff=payload.get("diff", []))

    if action == "find_unexpected_clear":
        search = await _dispatch_event("search_actions", {"session_id": session_id, "query": {"name_contains": "clear"}, "max_results": args.get("max_results", 200)})
        return search

    if action == "find_nan_inf_in_targets":
        texture_id = args.get("texture_id")
        if texture_id is None:
            texture_id = await _resolve_texture_id(session_id, None, event_id=_active_event(session_id))
        stats_resp = await _dispatch_texture("compute_stats", {"session_id": session_id, "texture_id": str(texture_id)})
        payload = json.loads(stats_resp)
        if not payload.get("success"):
            return stats_resp
        return _ok(result=payload.get("stats", {}))

    if action == "quick_triage_missing_draw":
        summary_resp = await _dispatch_macro("summarize_frame", {"session_id": session_id})
        diag_resp = await _dispatch_diag("scan_common_issues", {"session_id": session_id, "include_suggestions": True})
        return _ok(summary=json.loads(summary_resp), diagnostics=json.loads(diag_resp))

    if action == "build_bug_report_pack":
        _require(args, "output_path")
        output_path = Path(str(args["output_path"]))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        summary_resp = await _dispatch_macro("summarize_frame", {"session_id": session_id})
        summary_payload = json.loads(summary_resp)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("report/summary.json", json.dumps(summary_payload, ensure_ascii=False, indent=2))
        return _ok(saved_path=str(output_path))

    if action == "shader_hotfix_validate":
        _require(args, "replacement")
        repl_resp = await _dispatch_shader("edit_and_replace", {"session_id": session_id, **_as_dict(args["replacement"])})
        repl_payload = json.loads(repl_resp)
        if not repl_payload.get("success"):
            return repl_resp
        screenshot_before = await _dispatch_export("screenshot", {"session_id": session_id, "output_path": str(artifacts_dir() / "before_hotfix.png")})
        screenshot_after = await _dispatch_export("screenshot", {"session_id": session_id, "output_path": str(artifacts_dir() / "after_hotfix.png")})
        return _ok(replacement=repl_payload, before=json.loads(screenshot_before), after=json.loads(screenshot_after))

    return _err(f"Unsupported macro action: {action}")


async def _dispatch_analysis(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])
    if action == "get_frame_stats":
        summary_resp = await _dispatch_macro("summarize_frame", {"session_id": session_id, "frame_index": args.get("frame_index")})
        payload = json.loads(summary_resp)
        if not payload.get("success"):
            return summary_resp
        summary = payload.get("summary", {})
        stats = {
            "drawcalls": summary.get("draw_count", 0),
            "markers": summary.get("marker_count", 0),
            "resources": 0,
            "warnings": [],
        }
        return _ok(stats=stats)
    if action == "get_event_stats":
        _require(args, "event_id")
        detail_resp = await _dispatch_event("get_action_details", {"session_id": session_id, "event_id": args["event_id"]})
        payload = json.loads(detail_resp)
        if not payload.get("success"):
            return detail_resp
        action_obj = payload.get("action", {})
        return _ok(event_stats={"event_id": action_obj.get("event_id"), "name": action_obj.get("name"), "flags": action_obj.get("flags")})
    if action == "get_warnings":
        diag_resp = await _dispatch_diag("scan_common_issues", {"session_id": session_id, "severity_min": args.get("severity_min", "warn"), "include_suggestions": True})
        payload = json.loads(diag_resp)
        if not payload.get("success"):
            return diag_resp
        return _ok(warnings=payload.get("issues", []))
    if action == "estimate_overdraw":
        target = _parse_target_like(args.get("target"))
        texture_id = target.get("texture_id")
        if texture_id is None:
            texture_id = await _resolve_texture_id(session_id, None, event_id=_active_event(session_id))
        hist_resp = await _dispatch_texture("get_histogram", {"session_id": session_id, "texture_id": str(texture_id), "channels": "a", "bins": 16})
        payload = json.loads(hist_resp)
        if not payload.get("success"):
            return hist_resp
        overdraw = {"avg": 1.0, "max": 1.0, "histogram": payload.get("histogram", {}).get("a", {}).get("bins", [])}
        return _ok(overdraw=overdraw, heatmap_path=None)
    return _err(f"Unsupported analysis action: {action}")


async def _dispatch_util(action: str, args: Dict[str, Any]) -> str:
    if action == "compute_hash":
        _require(args, "path")
        algo = str(args.get("algo", "sha256")).lower()
        path = Path(str(args["path"]))
        if not path.is_file():
            return _err(f"Path not found: {path}")
        if algo not in {"sha256", "sha1", "md5"}:
            return _err(f"Unsupported hash algo: {algo}")
        h = hashlib.new(algo)
        with path.open("rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return _ok(hash=h.hexdigest(), algo=algo)

    if action == "diff_text":
        _require(args, "a", "b")
        a_is_path = _as_bool(args.get("a_is_path"), True)
        b_is_path = _as_bool(args.get("b_is_path"), True)
        a_text = Path(str(args["a"])).read_text(encoding="utf-8", errors="replace") if a_is_path else str(args["a"])
        b_text = Path(str(args["b"])).read_text(encoding="utf-8", errors="replace") if b_is_path else str(args["b"])
        context = _as_int(args.get("context_lines"), 3)
        diff = list(
            difflib.unified_diff(
                a_text.splitlines(),
                b_text.splitlines(),
                fromfile="a",
                tofile="b",
                n=context,
                lineterm="",
            ),
        )
        return _ok(diff=diff)

    if action == "diff_images":
        _require(args, "image_a_path", "image_b_path")
        try:
            import numpy as np
            from PIL import Image
        except Exception as exc:
            return _err(f"Image diff dependencies missing: {exc}")
        a = np.array(Image.open(str(args["image_a_path"])).convert("RGBA")).astype("float32") / 255.0
        b = np.array(Image.open(str(args["image_b_path"])).convert("RGBA")).astype("float32") / 255.0
        h = min(a.shape[0], b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a = a[:h, :w]
        b = b[:h, :w]
        diff = a - b
        mse = float((diff ** 2).mean()) if diff.size else 0.0
        max_abs = float(abs(diff).max()) if diff.size else 0.0
        psnr = float(20 * np.log10(1.0 / np.sqrt(mse))) if mse > 0 else float("inf")
        out = {"mse": mse, "max_abs": max_abs, "psnr": psnr}
        output_path = args.get("output_path")
        if output_path:
            diff_img = (np.clip(np.abs(diff), 0.0, 1.0) * 255.0).astype("uint8")
            out_path = Path(str(output_path))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(diff_img, mode="RGBA").save(out_path)
            out["diff_path"] = str(out_path)
        return _ok(metrics=out)

    if action == "pack_zip":
        _require(args, "paths", "output_path")
        paths = [Path(str(p)) for p in _as_list(args["paths"])]
        output = Path(str(args["output_path"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                if p.is_file():
                    zf.write(p, arcname=p.name)
                elif p.is_dir():
                    for child in p.rglob("*"):
                        if child.is_file():
                            zf.write(child, arcname=str(child.relative_to(p.parent)))
        return _ok(saved_path=str(output))

    if action == "list_artifacts":
        prefix = str(args.get("prefix", ""))
        artifacts = _artifact_store.list_artifacts(prefix=prefix)
        return _ok(artifacts=artifacts)

    if action == "cleanup_artifacts":
        result = _artifact_store.cleanup_artifacts(
            older_than_ms=args.get("older_than_ms"),
            prefix=str(args.get("prefix", "")),
            max_total_bytes=args.get("max_total_bytes"),
        )
        return _ok(**result)

    return _err(f"Unsupported util action: {action}")


async def _dispatch_remote(action: str, args: Dict[str, Any]) -> str:
    if action == "connect":
        _require(args, "host")
        host = str(args["host"])
        port = _as_int(args.get("port"), 38920)
        if not _runtime.enable_remote:
            return _err("Remote tools disabled by config")
        remote_id = _new_id("remote")
        _runtime.remotes[remote_id] = RemoteHandle(
            remote_id=remote_id,
            host=host,
            port=port,
            connected=False,
            detail={"requires_remote_device": True},
        )
        return _ok(remote_id=remote_id, detail={"requires_remote_device": True, "connected": False})
    if action == "disconnect":
        _require(args, "remote_id")
        _runtime.remotes.pop(str(args["remote_id"]), None)
        return _ok()
    if action in {
        "ping",
        "list_targets",
        "launch_app",
        "set_capture_options",
        "set_overlay_options",
        "trigger_capture",
        "queue_capture",
        "list_captures",
        "copy_capture",
        "delete_capture",
    }:
        _require(args, "remote_id")
        remote_id = str(args["remote_id"])
        if remote_id not in _runtime.remotes:
            return _err(f"Unknown remote_id: {remote_id}")
        return _err("Remote target interaction requires a live RenderDoc remote endpoint", requires_remote_device=True)
    return _err(f"Unsupported remote action: {action}")


async def _dispatch_app(action: str, args: Dict[str, Any]) -> str:
    connection = _as_dict(args.get("connection"), default={})
    conn_key = json.dumps(connection, sort_keys=True, ensure_ascii=False)
    if action == "is_available":
        return _ok(available=False, detail={"requires_app_integration": True, "connection": connection})
    if action in {"set_capture_option", "get_capture_options", "push_marker", "pop_marker", "set_marker", "start_frame_capture", "end_frame_capture", "trigger_capture"}:
        if action == "set_capture_option":
            _require(args, "option", "value")
            options = _runtime.app_capture_options.setdefault(conn_key, {})
            options[str(args["option"])] = args["value"]
            return _ok()
        if action == "get_capture_options":
            return _ok(options=_runtime.app_capture_options.get(conn_key, {}))
        return _err("App API requires in-process RenderDoc instrumentation", requires_app_integration=True)
    return _err(f"Unsupported app action: {action}")


_CATALOG_TOOLS = _load_tool_catalog()


def _build_tool_callable(tool_name: str, param_names: Sequence[str]) -> Any:
    unique: List[str] = []
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


if __name__ == "__main__":
    main()
