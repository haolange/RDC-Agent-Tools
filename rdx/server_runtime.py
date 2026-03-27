"""
RDX daemon/runtime server with registry-driven tool registration.

- Registers all catalog-defined tools from `rdx/spec/tool_catalog.json`
- Produces canonical shared envelopes with `ok/data/artifacts/error/meta`
"""

from __future__ import annotations

import asyncio
import contextvars
import ctypes
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
import time
import textwrap
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from rdx.config import RdxConfig
from rdx.context_snapshot import (
    SnapshotRetentionPolicy,
    clear_context_snapshot,
    default_context_snapshot,
    load_context_snapshot,
    merge_recent_artifacts,
    normalize_context_id,
    normalize_context_snapshot,
    normalize_pixel,
    save_context_snapshot,
    update_user_context,
)
from rdx.core.artifact_publisher import ArtifactPublisher
from rdx.core.contracts import TSV_FORMAT_VERSION, canonical_error, env_bool
from rdx.core.errors import CoreError, RuntimeToolError
from rdx.core.engine import CoreEngine, ExecutionContext
from rdx.core.renderdoc_status import (
    build_renderdoc_error_details,
    status_ok as _rd_status_ok,
    status_text as _rd_status_text,
)
from rdx.progress import ProgressReporter
from rdx.timeout_policy import PIXEL_HISTORY_TIMEOUT_S, remote_connect_timeout_ms
from rdx.core.event_graph import EventGraphService
from rdx.core.patch_engine import PatchEngine
from rdx.core.perf_service import PerfService
from rdx.core.pipeline_service import PipelineService
from rdx.core.render_service import RenderService
from rdx.core.session_manager import SessionError, SessionManager
from rdx.models import PatchSpec, ShaderStage, _new_id
from rdx.remote_bootstrap import (
    AndroidBootstrapOptions,
    AndroidRemoteBootstrapError,
    bootstrap_android_remote,
    cleanup_android_remote,
    describe_android_remote,
)
from rdx.runtime_bootstrap import bootstrap_renderdoc_runtime
from rdx.runtime_paths import artifacts_dir, ensure_runtime_dirs, runtime_root
from rdx.runtime_state import (
    append_runtime_log,
    clear_context_state,
    context_state_path,
    default_context_state,
    list_context_ids,
    load_context_state,
    read_runtime_logs,
    save_context_state,
    summarize_operation_durations,
)
from rdx.io_utils import atomic_write_json
from rdx.utils.artifact_store import ArtifactStore
from rdx.core.tsv_projection import project_rows, to_tsv_string

logger = logging.getLogger("rdx.server")
_CURRENT_CONTEXT_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar('rdx_current_context_id', default=None)
_CURRENT_PROGRESS_REPORTER: contextvars.ContextVar[ProgressReporter | None] = contextvars.ContextVar(
    "rdx_current_progress_reporter",
    default=None,
)


def _runtime_context_id() -> str:
    current = _CURRENT_CONTEXT_ID.get()
    if current:
        return normalize_context_id(current)
    return normalize_context_id(os.environ.get("RDX_CONTEXT_ID") or "default")


def _normalize_entry_mode(value: Any, *, default: str = "cli") -> str:
    mode = str(value or default).strip().lower()
    if mode not in {"cli", "mcp"}:
        return default
    return mode


def _normalize_backend(value: Any, *, default: str = "local") -> str:
    backend = str(value or default).strip().lower()
    if backend not in {"local", "remote"}:
        return default
    return backend


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
    transport: str = "renderdoc"
    remote_server: Any = None
    server_info: Dict[str, Any] = field(default_factory=dict)
    bootstrap: Dict[str, Any] = field(default_factory=dict)
    bootstrap_result: Any = None
    leased_session_id: str = ""
    leased_session_ids: List[str] = field(default_factory=list)
    requested_host: str = ""
    requested_port: int = 0
    device_serial: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    default_capture_options: Dict[str, Any] = field(default_factory=dict)
    overlay_options: Dict[str, Any] = field(default_factory=dict)
    known_targets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    known_captures: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    last_target_scan_ms: int = 0


@dataclass
class ConsumedRemoteHandle:
    remote_id: str
    endpoint: str
    transport: str = "renderdoc"
    consumed_by_session_id: str = ""
    consumed_at_ms: int = field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
    server_info: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ShaderDebugHandle:
    shader_debug_id: str
    session_id: str
    mode: str
    event_id: int
    trace: Any
    debugger: Any
    current_state: Any = None
    resolved_context: Dict[str, Any] = field(default_factory=dict)
    selected_target_source: str = ""
    pixel_history_summary: Dict[str, Any] = field(default_factory=dict)
    synthetic: bool = False
    synthetic_states: List[Any] = field(default_factory=list)
    synthetic_index: int = 0
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
    session_owned_remotes: Dict[str, RemoteHandle] = field(default_factory=dict)
    consumed_remotes: Dict[str, ConsumedRemoteHandle] = field(default_factory=dict)
    context_snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    context_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    hydrated_contexts: set[str] = field(default_factory=set)
    shader_debugs: Dict[str, ShaderDebugHandle] = field(default_factory=dict)
    shader_replacements: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    initialized: bool = False
    enable_remote: bool = True


_config: Optional[RdxConfig] = None
_session_manager: Optional[SessionManager] = None
_event_graph_service: Optional[EventGraphService] = None
_render_service: Optional[RenderService] = None
_pipeline_service: Optional[PipelineService] = None
_perf_service: Optional[PerfService] = None
_artifact_store: Optional[ArtifactStore] = None
_patch_engine: Optional[PatchEngine] = None
_runtime: RuntimeState = RuntimeState()
_runtime_bootstrapped: bool = False

_LIVE_OWNER_PREFIXES = (
    "rd.capture.",
    "rd.replay.",
    "rd.remote.",
    "rd.event.",
    "rd.pipeline.",
    "rd.shader.",
    "rd.texture.",
    "rd.resource.",
    "rd.buffer.",
    "rd.mesh.",
    "rd.export.",
    "rd.debug.",
    "rd.perf.",
)


def _current_progress_reporter() -> ProgressReporter | None:
    return _CURRENT_PROGRESS_REPORTER.get()


def _progress(
    stage: str,
    message: str,
    *,
    progress_pct: float | None = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    reporter = _current_progress_reporter()
    if reporter is None:
        return
    _record_operation_stage(
        _runtime_context_id(),
        trace_id=str(reporter.trace_id),
        stage=str(stage),
        message=str(message),
        progress_pct=progress_pct,
        details=details,
    )
    reporter.emit(stage, message, progress_pct=progress_pct, details=details)


def _snapshot_retention() -> SnapshotRetentionPolicy:
    cfg = _config or RdxConfig()
    return SnapshotRetentionPolicy(
        total_limit=int(getattr(cfg.snapshot_retention, "total_limit", 32) or 32),
        per_type_limit=int(getattr(cfg.snapshot_retention, "per_type_limit", 8) or 8),
    )


def _serialize_runtime_config() -> Dict[str, Any]:
    cfg = _config or RdxConfig()
    return {
        "artifact_dir": str(getattr(cfg.artifact, "store_path", artifacts_dir())),
        "temp_dir": str(runtime_root().resolve()),
        "log_level": str(getattr(cfg, "log_level", "INFO")).lower(),
        "snapshot_retention": {
            "total_limit": int(getattr(cfg.snapshot_retention, "total_limit", 32) or 32),
            "per_type_limit": int(getattr(cfg.snapshot_retention, "per_type_limit", 8) or 8),
        },
        "bisect": {
            "default_strategy": str(getattr(cfg.bisect, "default_strategy", "binary") or "binary"),
            "max_iterations": int(getattr(cfg.bisect, "max_iterations", 60) or 60),
            "default_confidence_threshold": float(
                getattr(cfg.bisect, "default_confidence_threshold", 0.85) or 0.85
            ),
            "confidence_profile": str(getattr(cfg.bisect, "confidence_profile", "default") or "default"),
        },
        "confidence_weights": {
            "sharpness": float(getattr(cfg.confidence_weights, "sharpness", 0.50) or 0.50),
            "consistency": float(getattr(cfg.confidence_weights, "consistency", 0.35) or 0.35),
            "range_factor": float(getattr(cfg.confidence_weights, "range_factor", 0.15) or 0.15),
        },
        "adaptive_bisect": {
            "mode": str(getattr(cfg.adaptive_bisect, "mode", "off") or "off"),
            "history_store_path": str(getattr(cfg.adaptive_bisect, "history_store_path", runtime_root() / "bisect_history.jsonl")),
        },
        "runtime_limits": {
            "max_contexts": int(getattr(cfg.runtime_limits, "max_contexts", 8) or 8),
            "max_sessions_per_context": int(getattr(cfg.runtime_limits, "max_sessions_per_context", 4) or 4),
            "max_capture_files": int(getattr(cfg.runtime_limits, "max_capture_files", 8) or 8),
            "max_capture_size_bytes": int(getattr(cfg.runtime_limits, "max_capture_size_bytes", 4 * 1024 * 1024 * 1024) or (4 * 1024 * 1024 * 1024)),
            "max_estimated_replay_memory_bytes": int(
                getattr(cfg.runtime_limits, "max_estimated_replay_memory_bytes", 8 * 1024 * 1024 * 1024)
                or (8 * 1024 * 1024 * 1024)
            ),
            "replay_memory_multiplier": float(getattr(cfg.runtime_limits, "replay_memory_multiplier", 3.0) or 3.0),
            "max_recent_operations": int(getattr(cfg.runtime_limits, "max_recent_operations", 64) or 64),
        },
    }


def _apply_runtime_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    global _config
    payload = dict(cfg or {})
    if _config is None:
        _config = RdxConfig.from_env()
    if "artifact_dir" in payload:
        artifact_dir = Path(str(payload.get("artifact_dir") or artifacts_dir())).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _config.artifact.store_path = artifact_dir
    if "log_level" in payload:
        _config.log_level = str(payload.get("log_level") or "INFO").upper()
    snapshot_retention = payload.get("snapshot_retention")
    if isinstance(snapshot_retention, dict):
        if "total_limit" in snapshot_retention:
            _config.snapshot_retention.total_limit = max(1, int(snapshot_retention.get("total_limit") or 32))
        if "per_type_limit" in snapshot_retention:
            _config.snapshot_retention.per_type_limit = max(1, int(snapshot_retention.get("per_type_limit") or 8))
    bisect_payload = payload.get("bisect")
    if isinstance(bisect_payload, dict):
        if "default_strategy" in bisect_payload:
            _config.bisect.default_strategy = str(bisect_payload.get("default_strategy") or "binary")
        if "max_iterations" in bisect_payload:
            _config.bisect.max_iterations = max(1, int(bisect_payload.get("max_iterations") or 60))
        if "default_confidence_threshold" in bisect_payload:
            _config.bisect.default_confidence_threshold = float(
                bisect_payload.get("default_confidence_threshold") or 0.85
            )
        if "confidence_profile" in bisect_payload:
            _config.bisect.confidence_profile = str(bisect_payload.get("confidence_profile") or "default")
    weight_payload = payload.get("confidence_weights")
    if isinstance(weight_payload, dict):
        sharpness = float(weight_payload.get("sharpness", _config.confidence_weights.sharpness))
        consistency = float(weight_payload.get("consistency", _config.confidence_weights.consistency))
        range_factor = float(weight_payload.get("range_factor", _config.confidence_weights.range_factor))
        total = sharpness + consistency + range_factor
        if sharpness < 0 or consistency < 0 or range_factor < 0 or total <= 0:
            raise ValueError("confidence_weights must be positive and sum to a non-zero value")
        _config.confidence_weights.sharpness = sharpness / total
        _config.confidence_weights.consistency = consistency / total
        _config.confidence_weights.range_factor = range_factor / total
    adaptive_payload = payload.get("adaptive_bisect")
    if isinstance(adaptive_payload, dict):
        if "mode" in adaptive_payload:
            _config.adaptive_bisect.mode = str(adaptive_payload.get("mode") or "off")
        if "history_store_path" in adaptive_payload:
            _config.adaptive_bisect.history_store_path = Path(
                str(adaptive_payload.get("history_store_path") or runtime_root() / "bisect_history.jsonl")
            )
    limits_payload = payload.get("runtime_limits")
    if isinstance(limits_payload, dict):
        if "max_contexts" in limits_payload:
            _config.runtime_limits.max_contexts = max(1, int(limits_payload.get("max_contexts") or 8))
        if "max_sessions_per_context" in limits_payload:
            _config.runtime_limits.max_sessions_per_context = max(1, int(limits_payload.get("max_sessions_per_context") or 4))
        if "max_capture_files" in limits_payload:
            _config.runtime_limits.max_capture_files = max(1, int(limits_payload.get("max_capture_files") or 8))
        if "max_capture_size_bytes" in limits_payload:
            _config.runtime_limits.max_capture_size_bytes = max(1, int(limits_payload.get("max_capture_size_bytes") or (4 * 1024 * 1024 * 1024)))
        if "max_estimated_replay_memory_bytes" in limits_payload:
            _config.runtime_limits.max_estimated_replay_memory_bytes = max(
                1,
                int(limits_payload.get("max_estimated_replay_memory_bytes") or (8 * 1024 * 1024 * 1024)),
            )
        if "replay_memory_multiplier" in limits_payload:
            _config.runtime_limits.replay_memory_multiplier = max(
                1.0,
                float(limits_payload.get("replay_memory_multiplier") or 3.0),
            )
        if "max_recent_operations" in limits_payload:
            _config.runtime_limits.max_recent_operations = max(1, int(limits_payload.get("max_recent_operations") or 64))
    _runtime.config = _serialize_runtime_config()
    return dict(_runtime.config)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _runtime_limits() -> Dict[str, Any]:
    cfg = _config or RdxConfig()
    return {
        "max_contexts": int(getattr(cfg.runtime_limits, "max_contexts", 8) or 8),
        "max_sessions_per_context": int(getattr(cfg.runtime_limits, "max_sessions_per_context", 4) or 4),
        "max_capture_files": int(getattr(cfg.runtime_limits, "max_capture_files", 8) or 8),
        "max_capture_size_bytes": int(getattr(cfg.runtime_limits, "max_capture_size_bytes", 4 * 1024 * 1024 * 1024) or (4 * 1024 * 1024 * 1024)),
        "max_estimated_replay_memory_bytes": int(
            getattr(cfg.runtime_limits, "max_estimated_replay_memory_bytes", 8 * 1024 * 1024 * 1024)
            or (8 * 1024 * 1024 * 1024)
        ),
        "replay_memory_multiplier": float(getattr(cfg.runtime_limits, "replay_memory_multiplier", 3.0) or 3.0),
        "max_recent_operations": int(getattr(cfg.runtime_limits, "max_recent_operations", 64) or 64),
    }


def _context_state_exists(context_id: Optional[str] = None) -> bool:
    return context_state_path(normalize_context_id(context_id or _runtime_context_id())).is_file()


def _ensure_context_capacity(context_id: Optional[str] = None) -> None:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    if _context_state_exists(ctx) or ctx in _runtime.context_states:
        return
    max_contexts = int(_runtime_limits().get("max_contexts", 8) or 8)
    existing = {normalize_context_id(item) for item in list_context_ids()}
    if len(existing) >= max_contexts:
        raise CoreError(
            code="context_limit_exceeded",
            message=f"Context limit exceeded for {ctx}",
            category="runtime",
            details={
                "context_id": ctx,
                "max_contexts": max_contexts,
                "known_contexts": sorted(existing),
            },
        )


def _context_state(context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _runtime.context_states.get(ctx)
    if not isinstance(state, dict):
        if not _context_state_exists(ctx):
            _ensure_context_capacity(ctx)
            state = default_context_state(ctx, limits=_runtime_limits())
        else:
            state = load_context_state(ctx, limits=_runtime_limits())
    state["limits"] = {**dict(state.get("limits") or {}), **_runtime_limits()}
    _runtime.context_states[ctx] = state
    return state


def _store_context_state(state: Dict[str, Any], context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    saved = save_context_state(state, ctx, limits=_runtime_limits())
    _runtime.context_states[ctx] = saved
    return saved


def _capture_file_metadata(file_path: str) -> Dict[str, Any]:
    path = Path(str(file_path or "")).resolve()
    if not path.is_file():
        return {
            "file_path": str(path),
            "file_size_bytes": 0,
            "file_mtime_ms": 0,
            "file_fingerprint": "",
        }
    stat = path.stat()
    return {
        "file_path": str(path),
        "file_size_bytes": int(stat.st_size),
        "file_mtime_ms": int(stat.st_mtime * 1000),
        "file_fingerprint": f"{int(stat.st_size)}:{int(stat.st_mtime * 1000)}",
    }


def _estimated_replay_memory_bytes(file_size_bytes: int) -> int:
    multiplier = float(_runtime_limits().get("replay_memory_multiplier", 3.0) or 3.0)
    return int(max(0, int(file_size_bytes or 0)) * max(multiplier, 1.0))


def _sync_context_metrics(context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    metrics = dict(state.get("metrics") or {})
    metrics["active_session_count"] = len([item for item in state.get("sessions", {}).values() if isinstance(item, dict)])
    metrics["active_capture_count"] = len([item for item in state.get("captures", {}).values() if isinstance(item, dict)])
    state["metrics"] = metrics
    return _store_context_state(state, ctx)


def _session_record_from_runtime(session_id: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    replay = _runtime.replays.get(str(session_id))
    if replay is None:
        raise KeyError(f"unknown session_id: {session_id}")
    state = _context_state(ctx)
    state_sessions = state.get("sessions", {}) if isinstance(state.get("sessions"), dict) else {}
    existing = dict(state_sessions.get(str(session_id)) or {})
    capture = _runtime.captures.get(replay.capture_file_id)
    capture_meta = _capture_file_metadata(capture.file_path if capture is not None else "")
    backend_type = str(existing.get("backend_type") or state.get("backend") or "").strip()
    if not backend_type:
        backend_type = "remote" if str(session_id) in _runtime.session_owned_remotes else "local"
    return {
        "session_id": str(session_id),
        "capture_file_id": str(replay.capture_file_id),
        "rdc_path": str(capture.file_path if capture is not None else ""),
        "file_fingerprint": str(capture_meta.get("file_fingerprint") or ""),
        "file_size_bytes": int(capture_meta.get("file_size_bytes") or 0),
        "frame_index": int(replay.frame_index or 0),
        "active_event_id": int(replay.active_event_id or 0),
        "backend_type": _normalize_backend(backend_type, default="local"),
        "state": "active",
        "is_live": True,
        "last_error": "",
        "updated_at_ms": _now_ms(),
        "recovery": {
            "status": "ready",
            "last_attempt_ms": 0,
            "last_success_ms": 0,
            "attempt_count": 0,
            "last_error": "",
        },
    }


def _capture_record_from_runtime(capture_file_id: str) -> Dict[str, Any]:
    capture = _runtime.captures.get(str(capture_file_id))
    if capture is None:
        raise KeyError(f"unknown capture_file_id: {capture_file_id}")
    meta = _capture_file_metadata(capture.file_path)
    return {
        "capture_file_id": str(capture.capture_file_id),
        "file_path": str(capture.file_path),
        "read_only": bool(capture.read_only),
        "driver": str(capture.driver or ""),
        "file_size_bytes": int(meta.get("file_size_bytes") or 0),
        "file_mtime_ms": int(meta.get("file_mtime_ms") or 0),
        "file_fingerprint": str(meta.get("file_fingerprint") or ""),
        "recovery_status": "ready",
        "last_error": "",
        "updated_at_ms": _now_ms(),
    }


def _select_session_from_state(
    state: Dict[str, Any],
    session_id: str = "",
    *,
    capture_file_id: str = "",
) -> Dict[str, Any]:
    sessions = state.get("sessions", {})
    captures = state.get("captures", {})
    chosen_session_id = str(session_id or state.get("current_session_id") or "").strip()
    chosen_capture_id = str(capture_file_id or state.get("current_capture_file_id") or "").strip()
    if chosen_session_id and chosen_session_id not in sessions:
        chosen_session_id = ""
    if not chosen_session_id and sessions:
        chosen_session_id = next(iter(sessions.keys()))
    if chosen_capture_id and chosen_capture_id not in captures:
        chosen_capture_id = ""
    if chosen_session_id and not chosen_capture_id:
        chosen_capture_id = str((sessions.get(chosen_session_id) or {}).get("capture_file_id") or "")
    if not chosen_capture_id and captures:
        chosen_capture_id = next(iter(captures.keys()))
    state["current_session_id"] = chosen_session_id
    state["current_capture_file_id"] = chosen_capture_id
    return state


def _infer_context_backend(snapshot: Dict[str, Any], state: Dict[str, Any]) -> str:
    backend = _normalize_backend(state.get("backend"), default="")
    if backend:
        return backend
    runtime_payload = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
    remote_payload = snapshot.get("remote") if isinstance(snapshot.get("remote"), dict) else {}
    if str(runtime_payload.get("backend_type") or "").strip() == "remote":
        return "remote"
    if str(remote_payload.get("state") or "none").strip() != "none":
        return "remote"
    return "local"


def _runtime_parallelism_ceiling_for_backend(backend: str) -> str:
    return "single_runtime_owner" if str(backend or "").strip() == "remote" else "multi_context_multi_owner"


def _coordination_projection(state: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    runtime_owner = dict(state.get("runtime_owner") or {})
    owner_lease = dict(state.get("owner_lease") or runtime_owner or {})
    backend = _infer_context_backend(snapshot, state)
    return {
        "entry_mode": _normalize_entry_mode(state.get("entry_mode"), default="cli"),
        "backend": backend,
        "runtime_parallelism_ceiling": _runtime_parallelism_ceiling_for_backend(backend),
        "runtime_owner": runtime_owner,
        "owner_lease": owner_lease,
        "active_baton": dict(state.get("active_baton") or {}),
        "rehydrate_status": dict(state.get("rehydrate_status") or {}),
    }


def _apply_coordination_projection(snapshot: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    projection = _coordination_projection(state, snapshot)
    snapshot["entry_mode"] = projection["entry_mode"]
    snapshot["backend"] = projection["backend"]
    snapshot["runtime_parallelism_ceiling"] = projection["runtime_parallelism_ceiling"]
    snapshot["runtime_owner"] = projection["runtime_owner"]
    snapshot["owner_lease"] = projection["owner_lease"]
    snapshot["active_baton"] = projection["active_baton"]
    snapshot["rehydrate_status"] = projection["rehydrate_status"]
    state["entry_mode"] = projection["entry_mode"]
    state["backend"] = projection["backend"]
    state["runtime_parallelism_ceiling"] = projection["runtime_parallelism_ceiling"]
    state["runtime_owner"] = projection["runtime_owner"]
    state["owner_lease"] = projection["owner_lease"]
    state["active_baton"] = projection["active_baton"]
    state["rehydrate_status"] = projection["rehydrate_status"]
    return snapshot


def _sync_context_snapshot_from_state(context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    state = _select_session_from_state(state)
    sessions = state.get("sessions", {})
    captures = state.get("captures", {})
    current_session = sessions.get(state.get("current_session_id")) if isinstance(sessions, dict) else None
    current_capture = captures.get(state.get("current_capture_file_id")) if isinstance(captures, dict) else None
    snapshot = _runtime.context_snapshots.get(ctx)
    if not isinstance(snapshot, dict):
        snapshot = load_context_snapshot(ctx, retention=_snapshot_retention())
    snapshot = normalize_context_snapshot(snapshot, ctx, retention=_snapshot_retention())
    if isinstance(current_session, dict):
        snapshot["runtime"].update(
            {
                "session_id": str(current_session.get("session_id") or ""),
                "capture_file_id": str(current_session.get("capture_file_id") or ""),
                "frame_index": int(current_session.get("frame_index") or 0),
                "active_event_id": int(current_session.get("active_event_id") or 0),
                "backend_type": str(current_session.get("backend_type") or "none"),
            }
        )
    elif isinstance(current_capture, dict):
        snapshot["runtime"].update(
            {
                "session_id": "",
                "capture_file_id": str(current_capture.get("capture_file_id") or ""),
                "frame_index": 0,
                "active_event_id": 0,
                "backend_type": "none",
            }
        )
    else:
        snapshot["runtime"] = default_context_snapshot(ctx).get("runtime", {})
    snapshot = _apply_coordination_projection(snapshot, state)
    _store_context_state(state, ctx)
    return _store_context_snapshot(snapshot, ctx)


def _upsert_context_capture(capture_file_id: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    state.setdefault("captures", {})[str(capture_file_id)] = _capture_record_from_runtime(capture_file_id)
    state = _select_session_from_state(state, capture_file_id=str(capture_file_id))
    _store_context_state(state, ctx)
    _sync_context_metrics(ctx)
    return _sync_context_snapshot_from_state(ctx)


def _remove_context_capture(capture_file_id: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    state.get("captures", {}).pop(str(capture_file_id), None)
    sessions = state.get("sessions", {})
    if isinstance(sessions, dict):
        for session_id, record in list(sessions.items()):
            if isinstance(record, dict) and str(record.get("capture_file_id") or "") == str(capture_file_id):
                sessions.pop(session_id, None)
    state = _select_session_from_state(state)
    _store_context_state(state, ctx)
    _sync_context_metrics(ctx)
    return _sync_context_snapshot_from_state(ctx)


def _upsert_context_session(session_id: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    record = _session_record_from_runtime(session_id, context_id=ctx)
    state.setdefault("sessions", {})[str(session_id)] = record
    capture_file_id = str(record.get("capture_file_id") or "")
    if capture_file_id and capture_file_id in _runtime.captures:
        state.setdefault("captures", {})[capture_file_id] = _capture_record_from_runtime(capture_file_id)
    state = _select_session_from_state(state, session_id=str(session_id), capture_file_id=capture_file_id)
    _store_context_state(state, ctx)
    _sync_context_metrics(ctx)
    return _sync_context_snapshot_from_state(ctx)


def _update_context_session_record(
    session_id: str,
    *,
    context_id: Optional[str] = None,
    backend_type: Optional[str] = None,
    frame_index: Optional[int] = None,
    active_event_id: Optional[int] = None,
    state_name: Optional[str] = None,
    is_live: Optional[bool] = None,
    last_error: Optional[str] = None,
    recovery_status: Optional[str] = None,
) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    sessions = state.setdefault("sessions", {})
    record = dict(sessions.get(str(session_id)) or {})
    if not record:
        try:
            record = _session_record_from_runtime(str(session_id), context_id=ctx)
        except KeyError as exc:
            raise CoreError(
                code="session_not_found",
                message=f"Unknown session_id: {session_id}",
                category="not_found",
                details={"session_id": str(session_id)},
            ) from exc
    resolved_backend = _normalize_backend(
        backend_type or record.get("backend_type") or state.get("backend"),
        default="local",
    )
    state["backend"] = resolved_backend
    record["backend_type"] = resolved_backend
    if frame_index is not None:
        record["frame_index"] = int(frame_index)
    if active_event_id is not None:
        record["active_event_id"] = int(active_event_id)
    if state_name is not None:
        record["state"] = str(state_name)
    if is_live is not None:
        record["is_live"] = bool(is_live)
    if last_error is not None:
        record["last_error"] = str(last_error)
    recovery = dict(record.get("recovery") or {})
    if recovery_status is not None:
        recovery["status"] = str(recovery_status)
    if last_error is not None:
        recovery["last_error"] = str(last_error)
    record["recovery"] = recovery
    record["updated_at_ms"] = _now_ms()
    sessions[str(session_id)] = record
    _store_context_state(state, ctx)
    return _sync_context_snapshot_from_state(ctx)


def _remove_context_session(session_id: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    state.get("sessions", {}).pop(str(session_id), None)
    state = _select_session_from_state(state)
    _store_context_state(state, ctx)
    _sync_context_metrics(ctx)
    return _sync_context_snapshot_from_state(ctx)


def _select_context_session_state(session_id: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    if str(session_id) not in state.get("sessions", {}):
        raise CoreError(
            code="session_not_found",
            message=f"Unknown session_id: {session_id}",
            category="not_found",
            details={"session_id": str(session_id)},
        )
    state = _select_session_from_state(state, session_id=str(session_id))
    _store_context_state(state, ctx)
    return _sync_context_snapshot_from_state(ctx)


def _trim_stage_events(stages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return list(stages or [])[-32:]


def _summarize_args(args: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"arg_keys": sorted(str(key) for key in args.keys())}
    for key, value in list(args.items())[:8]:
        if isinstance(value, (str, int, float, bool)) or value is None:
            summary[str(key)] = value
        elif isinstance(value, dict):
            summary[str(key)] = {"keys": sorted(str(item) for item in value.keys())[:8]}
        elif isinstance(value, list):
            summary[str(key)] = {"len": len(value)}
        else:
            summary[str(key)] = {"type": type(value).__name__}
    return summary


def _upsert_operation_entry(
    state: Dict[str, Any],
    trace_id: str,
    *,
    defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    recent = list(state.get("recent_operations") or [])
    existing = next((item for item in recent if isinstance(item, dict) and str(item.get("trace_id") or "") == str(trace_id)), None)
    if existing is None:
        existing = {
            "trace_id": str(trace_id),
            "operation": "",
            "transport": "",
            "status": "running",
            "args_summary": {},
            "stages": [],
            "result_ok": None,
            "error_code": "",
            "error_message": "",
            "duration_ms": 0,
            "started_at_ms": _now_ms(),
            "updated_at_ms": _now_ms(),
            "recovery_attempted": False,
        }
        recent.insert(0, existing)
    if isinstance(defaults, dict):
        for key, value in defaults.items():
            existing.setdefault(key, value)
    limit = int(state.get("limits", {}).get("max_recent_operations") or _runtime_limits().get("max_recent_operations", 64))
    state["recent_operations"] = [item for item in recent if isinstance(item, dict)]
    state["recent_operations"] = state["recent_operations"][: max(limit * 2, 8)]
    return existing


def _record_operation_start(
    context_id: str,
    *,
    trace_id: str,
    operation: str,
    transport: str,
    args: Dict[str, Any],
) -> None:
    ctx = normalize_context_id(context_id)
    state = _context_state(ctx)
    if transport in {"cli", "mcp"}:
        state["entry_mode"] = _normalize_entry_mode(transport, default=str(state.get("entry_mode") or "cli"))
    if str(operation or "").startswith("rd.remote."):
        state["backend"] = "remote"
    elif operation == "rd.capture.open_replay":
        options = args.get("options") if isinstance(args.get("options"), dict) else {}
        state["backend"] = "remote" if str(options.get("remote_id") or "").strip() else "local"
    entry = _upsert_operation_entry(
        state,
        str(trace_id),
        defaults={
            "operation": str(operation),
            "transport": str(transport),
            "args_summary": _summarize_args(args),
            "started_at_ms": _now_ms(),
        },
    )
    entry["operation"] = str(operation)
    entry["transport"] = str(transport)
    entry["args_summary"] = _summarize_args(args)
    entry["status"] = "running"
    entry["updated_at_ms"] = _now_ms()
    metrics = dict(state.get("metrics") or {})
    metrics["operation_count"] = int(metrics.get("operation_count") or 0) + 1
    metrics["last_operation_ms"] = _now_ms()
    state["metrics"] = metrics
    _store_context_state(state, ctx)


def _record_operation_stage(
    context_id: str,
    *,
    trace_id: str,
    stage: str,
    message: str,
    progress_pct: Optional[float] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    ctx = normalize_context_id(context_id)
    state = _context_state(ctx)
    entry = _upsert_operation_entry(state, str(trace_id))
    stages = list(entry.get("stages") or [])
    stages.append(
        {
            "stage": str(stage),
            "message": str(message),
            "progress_pct": progress_pct,
            "details": dict(details or {}),
            "ts_ms": _now_ms(),
        }
    )
    entry["stages"] = _trim_stage_events(stages)
    entry["updated_at_ms"] = _now_ms()
    _store_context_state(state, ctx)


def _record_operation_finish(
    context_id: str,
    *,
    trace_id: str,
    payload: Dict[str, Any],
) -> None:
    ctx = normalize_context_id(context_id)
    state = _context_state(ctx)
    entry = _upsert_operation_entry(state, str(trace_id))
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    ok = bool(payload.get("ok"))
    duration_ms = int(meta.get("duration_ms") or entry.get("duration_ms") or 0)
    entry["result_ok"] = ok
    entry["status"] = "completed" if ok else "failed"
    entry["error_code"] = str(error.get("code") or "")
    entry["error_message"] = str(error.get("message") or "")
    entry["duration_ms"] = duration_ms
    entry["updated_at_ms"] = _now_ms()
    metrics = dict(state.get("metrics") or {})
    if not ok:
        metrics["operation_error_count"] = int(metrics.get("operation_error_count") or 0) + 1
    durations = list(metrics.get("recent_operation_duration_ms") or [])
    if duration_ms >= 0:
        durations.append(duration_ms)
    metrics["recent_operation_duration_ms"] = durations[-64:]
    metrics["last_operation_ms"] = _now_ms()
    state["metrics"] = metrics
    limit = int(state.get("limits", {}).get("max_recent_operations") or _runtime_limits().get("max_recent_operations", 64))
    state["recent_operations"] = list(state.get("recent_operations") or [])[: max(limit, 1)]
    _store_context_state(state, ctx)


def _current_trace_details() -> Dict[str, str]:
    reporter = _current_progress_reporter()
    if reporter is None:
        return {"trace_id": "", "operation": ""}
    return {"trace_id": str(reporter.trace_id or ""), "operation": str(reporter.operation or "")}


def _record_log(level: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
    trace = _current_trace_details()
    ctx = normalize_context_id(_runtime_context_id())
    entry = {
        "ts_ms": _now_ms(),
        "level": level.lower(),
        "message": message,
        "context": context or {},
        "trace_id": trace["trace_id"],
        "operation": trace["operation"],
        "context_id": ctx,
    }
    _runtime.logs.append(entry)
    if len(_runtime.logs) > 5000:
        _runtime.logs = _runtime.logs[-5000:]
    try:
        append_runtime_log(ctx, entry)
    except Exception:
        pass


def _context_snapshot(context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    snapshot = _runtime.context_snapshots.get(ctx)
    if not isinstance(snapshot, dict):
        snapshot = load_context_snapshot(ctx, retention=_snapshot_retention())
    snapshot = normalize_context_snapshot(snapshot, ctx, retention=_snapshot_retention())
    state = _context_state(ctx)
    sessions = state.get("sessions", {}) if isinstance(state, dict) else {}
    captures = state.get("captures", {}) if isinstance(state, dict) else {}

    runtime_payload = snapshot.get("runtime", {})
    session_id = str(runtime_payload.get("session_id") or "").strip()
    capture_file_id = str(runtime_payload.get("capture_file_id") or "").strip()
    if session_id and session_id not in _runtime.replays:
        state_session = sessions.get(session_id) if isinstance(sessions, dict) else None
        if isinstance(state_session, dict):
            runtime_payload["session_id"] = str(state_session.get("session_id") or "")
            runtime_payload["capture_file_id"] = str(state_session.get("capture_file_id") or "")
            runtime_payload["frame_index"] = int(state_session.get("frame_index") or 0)
            runtime_payload["active_event_id"] = int(state_session.get("active_event_id") or 0)
            runtime_payload["backend_type"] = str(state_session.get("backend_type") or "none")
        else:
            runtime_payload["session_id"] = ""
            runtime_payload["frame_index"] = 0
            runtime_payload["active_event_id"] = 0
            runtime_payload["backend_type"] = "none"
    if capture_file_id and capture_file_id not in _runtime.captures:
        if capture_file_id in captures:
            runtime_payload["capture_file_id"] = capture_file_id
        else:
            runtime_payload["capture_file_id"] = ""

    current_session_id = str(state.get("current_session_id") or "").strip()
    if current_session_id and not runtime_payload.get("session_id"):
        state_session = sessions.get(current_session_id) if isinstance(sessions, dict) else None
        if isinstance(state_session, dict):
            runtime_payload["session_id"] = str(state_session.get("session_id") or "")
            runtime_payload["capture_file_id"] = str(state_session.get("capture_file_id") or "")
            runtime_payload["frame_index"] = int(state_session.get("frame_index") or 0)
            runtime_payload["active_event_id"] = int(state_session.get("active_event_id") or 0)
            runtime_payload["backend_type"] = str(state_session.get("backend_type") or "none")
    current_capture_file_id = str(state.get("current_capture_file_id") or "").strip()
    if current_capture_file_id and not runtime_payload.get("capture_file_id"):
        runtime_payload["capture_file_id"] = current_capture_file_id

    remote_payload = snapshot.get("remote", {})
    remote_state = str(remote_payload.get("state") or "none")
    remote_id = str(remote_payload.get("remote_id") or "")
    owned_session_id = str(remote_payload.get("consumed_by_session_id") or "")
    if remote_state == "live_handle" and (not remote_id or remote_id not in _runtime.remotes):
        snapshot["remote"] = default_context_snapshot(ctx).get("remote", {})
    elif remote_state == "live_handle" and remote_id in _runtime.remotes:
        live_handle = _runtime.remotes.get(remote_id)
        if live_handle is not None:
            snapshot["remote"] = _remote_snapshot_payload_from_handle(live_handle)
    elif remote_state == "session_owned" and owned_session_id not in _runtime.session_owned_remotes:
        state_session = sessions.get(owned_session_id) if isinstance(sessions, dict) else None
        state_remote = _session_record_remote_metadata(state_session) if isinstance(state_session, dict) else {}
        state_endpoint = str(state_remote.get("endpoint") or "")
        state_origin_remote_id = str(state_remote.get("origin_remote_id") or "")
        if state_remote and str(state_session.get("backend_type") or "") == "remote":
            snapshot["remote"] = {
                "state": "session_owned",
                "remote_id": "",
                "origin_remote_id": state_origin_remote_id or str(remote_payload.get("origin_remote_id") or ""),
                "endpoint": state_endpoint or str(remote_payload.get("endpoint") or ""),
                "consumed_by_session_id": owned_session_id,
                "active_session_ids": [owned_session_id] if owned_session_id else [],
            }
            _runtime.context_snapshots[ctx] = snapshot
            return snapshot
        origin_remote_id = str(remote_payload.get("origin_remote_id") or "")
        if origin_remote_id and origin_remote_id in _runtime.remotes:
            live_handle = _runtime.remotes.get(origin_remote_id)
            if live_handle is not None:
                snapshot["remote"] = _remote_snapshot_payload_from_handle(live_handle)
        elif origin_remote_id and origin_remote_id in _runtime.consumed_remotes:
            tombstone = _runtime.consumed_remotes[origin_remote_id]
            snapshot["remote"] = {
                "state": "consumed",
                "remote_id": "",
                "origin_remote_id": tombstone.remote_id,
                "endpoint": tombstone.endpoint,
                "consumed_by_session_id": tombstone.consumed_by_session_id,
                "active_session_ids": [],
            }
        else:
            snapshot["remote"] = default_context_snapshot(ctx).get("remote", {})

    snapshot = _apply_coordination_projection(snapshot, state)
    _store_context_state(state, ctx)
    _runtime.context_snapshots[ctx] = snapshot
    return snapshot



def _store_context_snapshot(snapshot: Dict[str, Any], context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    normalized = save_context_snapshot(snapshot, ctx, retention=_snapshot_retention())
    _runtime.context_snapshots[ctx] = normalized
    return normalized



def _set_context_capture_file(capture_file_id: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    return _upsert_context_capture(str(capture_file_id or ""), context_id=context_id)



def _clear_context_capture_file(capture_file_id: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    return _remove_context_capture(str(capture_file_id or ""), context_id=context_id)



def _set_context_runtime_session(
    session_id: str,
    *,
    capture_file_id: str,
    backend_type: str,
    frame_index: int,
    active_event_id: int,
    remote_metadata: Optional[Dict[str, Any]] = None,
    context_id: Optional[str] = None,
) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    sessions = state.setdefault("sessions", {})
    existing = dict(sessions.get(str(session_id)) or {})
    sessions[str(session_id)] = {
        **existing,
        "session_id": str(session_id or ""),
        "capture_file_id": str(capture_file_id or ""),
        "rdc_path": str((_runtime.captures.get(str(capture_file_id)) or CaptureFileHandle("", "", True)).file_path or existing.get("rdc_path") or ""),
        "file_fingerprint": str(existing.get("file_fingerprint") or _capture_file_metadata(str((_runtime.captures.get(str(capture_file_id)) or CaptureFileHandle("", "", True)).file_path or "")).get("file_fingerprint") or ""),
        "file_size_bytes": int(existing.get("file_size_bytes") or _capture_file_metadata(str((_runtime.captures.get(str(capture_file_id)) or CaptureFileHandle("", "", True)).file_path or "")).get("file_size_bytes") or 0),
        "frame_index": int(frame_index or 0),
        "active_event_id": int(active_event_id or 0),
        "backend_type": str(backend_type or "none"),
        "state": "active",
        "is_live": True,
        "last_error": "",
        "updated_at_ms": _now_ms(),
        "remote": dict(remote_metadata or existing.get("remote") or {}),
        "recovery": {
            "status": "ready",
            "last_attempt_ms": int(existing.get("recovery", {}).get("last_attempt_ms") if isinstance(existing.get("recovery"), dict) else 0 or 0),
            "last_success_ms": int(existing.get("recovery", {}).get("last_success_ms") if isinstance(existing.get("recovery"), dict) else 0 or 0),
            "attempt_count": int(existing.get("recovery", {}).get("attempt_count") if isinstance(existing.get("recovery"), dict) else 0 or 0),
            "last_error": "",
        },
    }
    if str(capture_file_id or "") in _runtime.captures:
        state.setdefault("captures", {})[str(capture_file_id)] = _capture_record_from_runtime(str(capture_file_id))
    state = _select_session_from_state(state, session_id=str(session_id), capture_file_id=str(capture_file_id))
    _store_context_state(state, ctx)
    _sync_context_metrics(ctx)
    return _sync_context_snapshot_from_state(ctx)



def _set_context_active_event(session_id: str, event_id: int, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    return _update_context_session_record(
        str(session_id or ""),
        context_id=context_id,
        active_event_id=int(event_id or 0),
    )



def _set_context_frame(session_id: str, frame_index: int, active_event_id: int, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    return _update_context_session_record(
        str(session_id or ""),
        context_id=context_id,
        frame_index=int(frame_index or 0),
        active_event_id=int(active_event_id or 0),
    )



def _remote_active_session_ids(handle: Optional[RemoteHandle]) -> List[str]:
    if handle is None:
        return []
    session_ids = [str(item or "").strip() for item in list(handle.leased_session_ids or [])]
    session_ids = [item for item in session_ids if item]
    leased_session_id = str(handle.leased_session_id or "").strip()
    if leased_session_id and leased_session_id not in session_ids:
        session_ids.append(leased_session_id)
    return session_ids


def _remote_snapshot_payload_from_handle(handle: RemoteHandle) -> Dict[str, Any]:
    active_session_ids = _remote_active_session_ids(handle)
    return {
        "state": "live_handle",
        "remote_id": str(handle.remote_id or ""),
        "origin_remote_id": str(handle.remote_id or ""),
        "endpoint": _remote_url(handle.host, handle.port),
        "consumed_by_session_id": active_session_ids[0] if active_session_ids else "",
        "active_session_ids": active_session_ids,
    }



def _set_context_remote_live(remote_id: str, endpoint: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    state["backend"] = "remote"
    _store_context_state(state, ctx)
    snapshot = _context_snapshot(ctx)
    handle = _runtime.remotes.get(str(remote_id or "").strip())
    if handle is not None:
        payload = _remote_snapshot_payload_from_handle(handle)
        payload["endpoint"] = str(endpoint or payload.get("endpoint") or "")
        snapshot["remote"] = payload
    else:
        snapshot["remote"] = {
            "state": "live_handle",
            "remote_id": str(remote_id or ""),
            "origin_remote_id": str(remote_id or ""),
            "endpoint": str(endpoint or ""),
            "consumed_by_session_id": "",
            "active_session_ids": [],
        }
    return _store_context_snapshot(snapshot, ctx)



def _set_context_remote_session_owned(
    remote_id: str,
    session_id: str,
    endpoint: str,
    *,
    context_id: Optional[str] = None,
) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    state["backend"] = "remote"
    _store_context_state(state, ctx)
    snapshot = _context_snapshot(ctx)
    snapshot["remote"] = {
        "state": "session_owned",
        "remote_id": "",
        "origin_remote_id": str(remote_id or ""),
        "endpoint": str(endpoint or ""),
        "consumed_by_session_id": str(session_id or ""),
        "active_session_ids": [str(session_id or "")] if str(session_id or "").strip() else [],
    }
    return _store_context_snapshot(snapshot, ctx)



def _set_context_remote_consumed(
    remote_id: str,
    session_id: str,
    endpoint: str,
    *,
    context_id: Optional[str] = None,
) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _context_state(ctx)
    state["backend"] = "remote"
    _store_context_state(state, ctx)
    snapshot = _context_snapshot(ctx)
    snapshot["remote"] = {
        "state": "consumed",
        "remote_id": "",
        "origin_remote_id": str(remote_id or ""),
        "endpoint": str(endpoint or ""),
        "consumed_by_session_id": str(session_id or ""),
        "active_session_ids": [],
    }
    return _store_context_snapshot(snapshot, ctx)



def _register_remote_session_lease(
    session_id: str,
    handle: RemoteHandle,
    *,
    context_id: Optional[str] = None,
) -> RemoteHandle:
    sid = str(session_id or "").strip()
    if not sid:
        return handle
    active_session_ids = _remote_active_session_ids(handle)
    if sid not in active_session_ids:
        active_session_ids.append(sid)
    handle.leased_session_ids = active_session_ids
    handle.leased_session_id = active_session_ids[0] if active_session_ids else ""
    _runtime.session_owned_remotes[sid] = handle
    if handle.remote_id in _runtime.remotes:
        _set_context_remote_live(handle.remote_id, _remote_url(handle.host, handle.port), context_id=context_id)
    else:
        _set_context_remote_session_owned(handle.remote_id, sid, _remote_url(handle.host, handle.port), context_id=context_id)
    return handle



def _release_remote_session_lease(session_id: str, *, context_id: Optional[str] = None) -> Optional[RemoteHandle]:
    sid = str(session_id or "").strip()
    handle = _runtime.session_owned_remotes.pop(sid, None)
    if handle is None:
        return None
    active_session_ids = [item for item in _remote_active_session_ids(handle) if item != sid]
    handle.leased_session_ids = active_session_ids
    handle.leased_session_id = active_session_ids[0] if active_session_ids else ""
    if handle.remote_id in _runtime.remotes:
        _set_context_remote_live(handle.remote_id, _remote_url(handle.host, handle.port), context_id=context_id)
    elif active_session_ids:
        _set_context_remote_session_owned(
            handle.remote_id,
            active_session_ids[0],
            _remote_url(handle.host, handle.port),
            context_id=context_id,
        )
    return handle



def _clear_context_remote_live(remote_id: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    snapshot = _context_snapshot(ctx)
    remote = snapshot.get("remote", {})
    if remote.get("state") == "live_handle" and remote.get("remote_id") == str(remote_id or ""):
        snapshot["remote"] = default_context_snapshot(context_id).get("remote", {})
        state = _context_state(ctx)
        if str((snapshot.get("runtime") or {}).get("backend_type") or "none") != "remote":
            state["backend"] = "local"
            _store_context_state(state, ctx)
        return _store_context_snapshot(snapshot, ctx)
    return snapshot



def _clear_context_runtime(session_id: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    snapshot = _context_snapshot(ctx)
    remote = snapshot.get("remote", {})
    if remote.get("state") == "live_handle":
        active_session_ids = [
            item
            for item in [str(value or "").strip() for value in list(remote.get("active_session_ids") or [])]
            if item and item != str(session_id or "").strip()
        ]
        if active_session_ids or str(remote.get("remote_id") or "").strip():
            snapshot["remote"]["active_session_ids"] = active_session_ids
            snapshot["remote"]["consumed_by_session_id"] = active_session_ids[0] if active_session_ids else ""
            _store_context_snapshot(snapshot, ctx)
    elif remote.get("state") == "session_owned" and remote.get("consumed_by_session_id") == str(session_id or ""):
        snapshot["remote"]["state"] = "consumed"
        snapshot["remote"]["active_session_ids"] = []
        _store_context_snapshot(snapshot, ctx)
    result = _remove_context_session(str(session_id or ""), context_id=ctx)
    state = _context_state(ctx)
    if not any(str((record or {}).get("backend_type") or "") == "remote" for record in (state.get("sessions") or {}).values()):
        state["backend"] = "local"
        _store_context_state(state, ctx)
        result = _sync_context_snapshot_from_state(ctx)
    return result



def _reset_context_snapshot(context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    clear_context_state(ctx)
    _runtime.context_states.pop(ctx, None)
    snapshot = default_context_snapshot(ctx)
    return _store_context_snapshot(snapshot, ctx)



def _append_context_artifacts(artifacts: Sequence[Dict[str, Any]], source_tool: str, *, context_id: Optional[str] = None) -> Dict[str, Any]:
    if not artifacts:
        return _context_snapshot(context_id)
    snapshot = _context_snapshot(context_id)
    snapshot = merge_recent_artifacts(snapshot, artifacts, source_tool=source_tool, retention=_snapshot_retention())
    return _store_context_snapshot(snapshot, context_id)



def _sync_focus_from_args(operation: str, args: Dict[str, Any], *, context_id: Optional[str] = None) -> Dict[str, Any]:
    snapshot = _context_snapshot(context_id)
    changed = False
    if operation in {"rd.macro.explain_pixel", "rd.debug.pixel_history"}:
        if args.get("x") is not None and args.get("y") is not None:
            pixel = {"x": int(args.get("x") or 0), "y": int(args.get("y") or 0)}
            target = args.get("target")
            if isinstance(target, dict):
                pixel["target"] = dict(target)
            snapshot["focus"]["pixel"] = pixel
            changed = True
    for key in ("resource_id", "texture_id"):
        if args.get(key):
            snapshot["focus"]["resource_id"] = str(args.get(key) or "")
            changed = True
            break
    if args.get("shader_id"):
        snapshot["focus"]["shader_id"] = str(args.get("shader_id") or "")
        changed = True
    if changed:
        return _store_context_snapshot(snapshot, context_id)
    return snapshot



def _remote_consumed_payload(remote_id: str) -> str | None:
    tombstone = _runtime.consumed_remotes.get(str(remote_id or ""))
    if tombstone is None:
        return None
    details = {
        "remote_id": tombstone.remote_id,
        "endpoint": tombstone.endpoint,
        "consumed_by_session_id": tombstone.consumed_by_session_id,
        "consumed_at_ms": tombstone.consumed_at_ms,
        "source_layer": "runtime",
        "operation": "remote_handle_lifecycle",
        "backend_type": "remote",
        "capture_context": {
            "remote_id": tombstone.remote_id,
            "session_id": tombstone.consumed_by_session_id,
        },
        "classification": "tool_usage_conflict",
        "fix_hint": "Reconnect with rd.remote.connect to obtain a new live remote_id.",
    }
    return _err(
        f"Remote handle {tombstone.remote_id} has been consumed by session {tombstone.consumed_by_session_id}",
        code="remote_handle_consumed",
        category="runtime",
        details=details,
    )



def _postprocess_context_snapshot(operation: str, args: Dict[str, Any], payload: Dict[str, Any], ctx: ExecutionContext) -> None:
    context_id = normalize_context_id((ctx.metadata or {}).get("context_id") or _runtime_context_id())
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else []
    if isinstance(artifacts, list) and artifacts:
        _append_context_artifacts(artifacts, operation, context_id=context_id)
    _sync_focus_from_args(operation, args, context_id=context_id)


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


def _projection_request(args: Dict[str, Any], tool_name: str) -> Dict[str, Any]:
    projection = args.get("projection")
    if projection is None:
        return {}
    if not isinstance(projection, dict):
        raise ValueError(f"{tool_name} projection must be an object")
    kind = str(projection.get("kind") or "").strip().lower()
    if kind and kind != "tabular":
        raise ValueError(f"{tool_name} only supports projection.kind='tabular'")
    return {
        "kind": "tabular",
        "include_tsv_text": _as_bool(projection.get("include_tsv_text"), False),
    }


def _tabular_projection(
    rows: Sequence[Dict[str, Any]],
    *,
    columns: Sequence[str],
    include_tsv_text: bool,
    details_json_path: str = "",
    details_json_url: str = "",
) -> Dict[str, Any]:
    normalized_rows = [
        {
            **dict(row),
            "details_json_path": str(row.get("details_json_path") or details_json_path),
            "details_json_url": str(row.get("details_json_url") or details_json_url),
        }
        for row in rows
    ]
    header, body = project_rows(normalized_rows, columns=columns, format_version=TSV_FORMAT_VERSION)
    tabular: Dict[str, Any] = {
        "format_version": TSV_FORMAT_VERSION,
        "columns": header,
        "rows": body,
        "row_count": len(body),
    }
    if details_json_path:
        tabular["details_json_path"] = str(details_json_path)
    if details_json_url:
        tabular["details_json_url"] = str(details_json_url)
    if include_tsv_text:
        tabular["tsv_text"] = to_tsv_string(normalized_rows, columns=columns, format_version=TSV_FORMAT_VERSION)
    return {"tabular": tabular}


def _vfs_entries_projection(entries: Sequence[Dict[str, Any]], *, include_tsv_text: bool) -> Dict[str, Any]:
    rows = [
        {
            "name": str(entry.get("name") or ""),
            "path": str(entry.get("path") or ""),
            "kind": str(entry.get("kind") or ""),
            "title": str(entry.get("title") or ""),
            "summary": str(entry.get("summary") or ""),
            "requires_session": bool(entry.get("requires_session", False)),
            "exists": bool(entry.get("exists", True)),
        }
        for entry in entries
        if isinstance(entry, dict)
    ]
    return _tabular_projection(
        rows,
        columns=["name", "path", "kind", "title", "summary", "requires_session", "exists"],
        include_tsv_text=include_tsv_text,
    )


def _artifact_rows_projection(artifacts: Sequence[Dict[str, Any]], *, include_tsv_text: bool) -> Dict[str, Any]:
    rows = [
        {
            "artifact_id": str(item.get("artifact_id") or ""),
            "type": str(item.get("type") or ""),
            "path": str(item.get("path") or ""),
            "url": str(item.get("url") or ""),
            "mime": str(item.get("mime") or ""),
            "size_bytes": int(item.get("size_bytes") or 0),
            "storage_backend": str(item.get("storage_backend") or ""),
        }
        for item in artifacts
        if isinstance(item, dict)
    ]
    return _tabular_projection(
        rows,
        columns=["artifact_id", "type", "path", "url", "mime", "size_bytes", "storage_backend"],
        include_tsv_text=include_tsv_text,
    )


def _capability_entry(
    available: bool,
    *,
    reason: str,
    optional: bool,
    source: str,
    **extra: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "available": bool(available),
        "reason": str(reason or ""),
        "optional": bool(optional),
        "source": str(source),
    }
    payload.update(extra)
    return payload


def _capability_error(
    code: str,
    message: str,
    *,
    capability: str,
    reason: str,
    source: str,
    optional: bool = True,
    **details: Any,
) -> str:
    return _err(
        message,
        code=code,
        category="capability",
        details={
            "capability": capability,
            "reason": reason,
            "optional": bool(optional),
            "source": source,
            **details,
        },
    )


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


def _status_ok(status: Any) -> bool:
    return _rd_status_ok(status, _get_rd())


def _status_text(status: Any) -> str:
    return _rd_status_text(status)


def _check_status(
    status: Any,
    operation: str,
    *,
    backend_type: str = "local",
    capture_context: Optional[Dict[str, Any]] = None,
    classification: str = "renderdoc_status",
    fix_hint: str = "Inspect the RenderDoc status and capture context before retrying.",
) -> None:
    if not _status_ok(status):
        details = build_renderdoc_error_details(
            status,
            operation=operation,
            source_layer="renderdoc_status",
            backend_type=backend_type,
            capture_context=capture_context,
            classification=classification,
            fix_hint=fix_hint,
        )
        raise RuntimeToolError(
            f"{operation} failed with status: {details['renderdoc_status']['status_text']}",
            details=details,
        )


def _renderdoc_version_value() -> str:
    try:
        rd = _get_rd()
        if hasattr(rd, "GetVersionString"):
            return str(rd.GetVersionString())
    except Exception:
        pass
    return "unknown"


def _remote_url(host: str, port: int) -> str:
    return f"{host}:{port}" if int(port or 0) > 0 else str(host)


def _wait_for_remote_endpoint(url: str, timeout_ms: int) -> None:
    rd = _get_rd()
    timeout_s = max(int(timeout_ms or 0), 1) / 1000.0
    deadline = time.perf_counter() + timeout_s
    last_status: Any = None
    last_progress_ts = 0.0
    while True:
        status = rd.CheckRemoteServerConnection(url)
        if _status_ok(status):
            return
        last_status = status
        now = time.perf_counter()
        if (now - last_progress_ts) >= 2.0:
            last_progress_ts = now
            _progress(
                "waiting_endpoint",
                "Remote endpoint is still unavailable, retrying",
                progress_pct=0.35,
                details={"endpoint": url},
            )
        if time.perf_counter() >= deadline:
            details = build_renderdoc_error_details(
                status if last_status is None else last_status,
                operation=f"CheckRemoteServerConnection({url})",
                source_layer="renderdoc_status",
                backend_type="remote",
                capture_context={"endpoint": url},
                classification="remote_endpoint",
                fix_hint="Verify the remote endpoint is reachable before opening a remote replay session.",
            )
            raise RuntimeToolError(
                f"CheckRemoteServerConnection({url}) failed: {details['renderdoc_status']['status_text']}",
                details=details,
            )
        time.sleep(min(0.25, max(deadline - time.perf_counter(), 0.05)))


def _create_remote_server_connection(url: str) -> Any:
    rd = _get_rd()
    status, remote = rd.CreateRemoteServerConnection(url)
    if not _status_ok(status) or remote is None:
        details = build_renderdoc_error_details(
            status,
            operation=f"CreateRemoteServerConnection({url})",
            source_layer="renderdoc_status",
            backend_type="remote",
            capture_context={"endpoint": url},
            classification="remote_endpoint",
            fix_hint="Reconnect to the remote endpoint and confirm it still exposes a RenderDoc server.",
        )
        raise RuntimeToolError(
            f"CreateRemoteServerConnection({url}) failed: {details['renderdoc_status']['status_text']}",
            details=details,
        )
    return remote


def _collect_remote_server_info(
    remote_server: Any,
    *,
    host: str,
    port: int,
    transport: str,
    bootstrap: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "version": _renderdoc_version_value(),
        "name": str(host),
        "platform": "android" if transport == "adb_android" else "unknown",
        "transport": str(transport),
        "endpoint": _remote_url(host, port),
    }
    try:
        info["driver_name"] = str(remote_server.DriverName() or "")
    except Exception:
        info["driver_name"] = ""
    try:
        replays = remote_server.RemoteSupportedReplays()
        info["capabilities"] = {
            "supported_replays": [str(item) for item in list(replays or [])],
        }
    except Exception:
        info.setdefault("capabilities", {})
    if bootstrap:
        info["bootstrap"] = dict(bootstrap)
        if transport == "adb_android":
            info["platform"] = "android"
    return info


def _disconnect_remote_handle_sync(handle: RemoteHandle) -> List[str]:
    errors: List[str] = []
    if handle.remote_server is not None:
        try:
            if handle.transport == "adb_android" and hasattr(handle.remote_server, "ShutdownServerAndConnection"):
                handle.remote_server.ShutdownServerAndConnection()
            else:
                handle.remote_server.ShutdownConnection()
        except Exception as exc:
            errors.append(f"remote shutdown failed: {exc}")
    if handle.transport == "adb_android" and handle.bootstrap_result is not None:
        errors.extend(cleanup_android_remote(handle.bootstrap_result))
    return errors


_CAPTURE_OPTION_ATTRS: Dict[str, str] = {
    "allow_vsync": "allowVSync",
    "allow_fullscreen": "allowFullscreen",
    "api_validation": "apiValidation",
    "capture_all_cmd_lists": "captureAllCmdLists",
    "capture_callstacks": "captureCallstacks",
    "capture_callstacks_only_actions": "captureCallstacksOnlyActions",
    "debug_output_mute": "debugOutputMute",
    "delay_for_debugger": "delayForDebugger",
    "hook_into_children": "hookIntoChildren",
    "ref_all_resources": "refAllResources",
    "soft_memory_limit": "softMemoryLimit",
    "verify_buffer_access": "verifyBufferAccess",
}


def _remote_target_message_type_name(value: Any) -> str:
    rd = _get_rd()
    raw_value = int(value or 0)
    for name in dir(rd.TargetControlMessageType):
        if name.startswith("_"):
            continue
        try:
            if int(getattr(rd.TargetControlMessageType, name)) == raw_value:
                return name
        except Exception:
            continue
    return str(raw_value)


def _capture_options_payload(options: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, attr in _CAPTURE_OPTION_ATTRS.items():
        if hasattr(options, attr):
            payload[key] = getattr(options, attr)
    return payload


def _capture_options_from_dict(values: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    rd = _get_rd()
    options = rd.GetDefaultCaptureOptions()
    applied: Dict[str, Any] = {}
    for raw_key, raw_value in dict(values or {}).items():
        key = str(raw_key or "").strip()
        attr = _CAPTURE_OPTION_ATTRS.get(key, key)
        if not attr or not hasattr(options, attr):
            continue
        current_value = getattr(options, attr)
        if isinstance(current_value, bool):
            coerced = _as_bool(raw_value, current_value)
        elif isinstance(current_value, int):
            coerced = _as_int(raw_value, int(current_value))
        else:
            coerced = raw_value
        setattr(options, attr, coerced)
        applied[key] = coerced
    return options, applied


def _environment_modifications_from_dict(values: Dict[str, Any]) -> List[Any]:
    rd = _get_rd()
    modifications: List[Any] = []
    for name, value in dict(values or {}).items():
        item = rd.EnvironmentModification()
        item.name = str(name or "")
        item.value = str(value or "")
        if hasattr(item, "sep"):
            item.sep = ";"
        if hasattr(item, "mod"):
            try:
                item.mod = 0
            except Exception:
                pass
        modifications.append(item)
    return modifications


def _remote_target_control_client_name(handle: RemoteHandle) -> str:
    remote_id = str(handle.remote_id or "remote").strip() or "remote"
    return f"rdx-tools-{remote_id}"[:64]


def _remote_target_ident(target_id: Any) -> int:
    text = str(target_id or "").strip()
    try:
        return int(text)
    except Exception:
        return 0


def _create_target_control_sync(handle: RemoteHandle, ident: int, *, force_connection: bool = False) -> Any:
    rd = _get_rd()
    return rd.CreateTargetControl(
        _remote_url(handle.host, handle.port),
        int(ident),
        _remote_target_control_client_name(handle),
        bool(force_connection),
    )


def _target_summary_from_control(handle: RemoteHandle, ident: int, control: Any) -> Dict[str, Any]:
    target_id = str(int(ident))
    target = dict(handle.known_targets.get(target_id) or {})
    target.update(
        {
            "target_id": target_id,
            "ident": int(ident),
            "name": str(control.GetTarget() or ""),
            "api": str(control.GetAPI() or ""),
            "pid": int(control.GetPID() or 0),
            "connected": bool(control.Connected()) if hasattr(control, "Connected") else True,
        }
    )
    busy_client = str(control.GetBusyClient() or "") if hasattr(control, "GetBusyClient") else ""
    if busy_client:
        target["busy_client"] = busy_client
    handle.known_targets[target_id] = target
    return target


def _update_remote_capture_registry_from_message(
    handle: RemoteHandle,
    *,
    target_id: str,
    message_type: str,
    message: Any,
) -> Optional[Dict[str, Any]]:
    capture = getattr(message, "newCapture", None)
    capture_id = int(getattr(capture, "captureId", 0) or 0)
    if capture_id <= 0:
        return None
    capture_key = str(capture_id)
    record = dict(handle.known_captures.get(capture_key) or {})
    record.update(
        {
            "capture_id": capture_key,
            "target_id": str(target_id or ""),
            "path": str(getattr(capture, "path", "") or ""),
            "title": str(getattr(capture, "title", "") or ""),
            "api": str(getattr(capture, "api", "") or ""),
            "frame_number": int(getattr(capture, "frameNumber", 0) or 0),
            "size_bytes": int(getattr(capture, "byteSize", 0) or 0),
            "timestamp": int(getattr(capture, "timestamp", 0) or 0),
            "local": bool(getattr(capture, "local", False)),
            "thumb_width": int(getattr(capture, "thumbWidth", 0) or 0),
            "thumb_height": int(getattr(capture, "thumbHeight", 0) or 0),
            "message_type": message_type,
            "updated_at_ms": _now_ms(),
        }
    )
    handle.known_captures[capture_key] = record
    return record


def _drain_target_control_messages_sync(
    handle: RemoteHandle,
    control: Any,
    *,
    target_id: str,
    max_messages: int = 16,
    deadline_s: float = 0.0,
) -> List[Dict[str, Any]]:
    rd = _get_rd()
    events: List[Dict[str, Any]] = []
    deadline = time.perf_counter() + max(float(deadline_s or 0.0), 0.0)
    remaining_messages = max(1, int(max_messages or 1))
    while remaining_messages > 0:
        message = control.ReceiveMessage(None)
        message_type = _remote_target_message_type_name(getattr(message, "type", 0))
        event: Dict[str, Any] = {"type": message_type}
        if message_type == "Noop":
            if deadline_s > 0.0 and time.perf_counter() < deadline:
                time.sleep(0.1)
                remaining_messages -= 1
                continue
            break
        if message_type == "Disconnected":
            events.append(event)
            break
        if message_type in {"NewCapture", "CaptureCopied"}:
            capture_record = _update_remote_capture_registry_from_message(
                handle,
                target_id=target_id,
                message_type=message_type,
                message=message,
            )
            if capture_record is not None:
                event["capture"] = capture_record
        elif message_type == "NewChild":
            child = getattr(message, "newChild", None)
            child_ident = int(getattr(child, "ident", 0) or 0)
            if child_ident > 0:
                child_target_id = str(child_ident)
                handle.known_targets.setdefault(
                    child_target_id,
                    {
                        "target_id": child_target_id,
                        "ident": child_ident,
                        "pid": int(getattr(child, "processId", 0) or 0),
                    },
                )
                event["target"] = dict(handle.known_targets[child_target_id])
        elif message_type == "RegisterAPI":
            api_use = getattr(message, "apiUse", None)
            target = dict(handle.known_targets.get(str(target_id or "")) or {})
            target["api"] = str(getattr(api_use, "name", "") or target.get("api") or "")
            target["supported"] = bool(getattr(api_use, "supported", True))
            support_message = str(getattr(api_use, "supportMessage", "") or "")
            if support_message:
                target["support_message"] = support_message
            target["presenting"] = bool(getattr(api_use, "presenting", False))
            handle.known_targets[str(target_id or "")] = target
            event["target"] = target
        elif message_type == "Busy":
            busy = getattr(message, "busy", None)
            target = dict(handle.known_targets.get(str(target_id or "")) or {})
            target["busy_client"] = str(getattr(busy, "clientName", "") or "")
            handle.known_targets[str(target_id or "")] = target
            event["target"] = target
        elif message_type == "CapturableWindowCount":
            target = dict(handle.known_targets.get(str(target_id or "")) or {})
            target["capturable_window_count"] = int(getattr(message, "capturableWindowCount", 0) or 0)
            handle.known_targets[str(target_id or "")] = target
            event["target"] = target
        elif message_type == "CaptureProgress":
            event["progress"] = float(getattr(message, "capProgress", 0.0) or 0.0)
        events.append(event)
        remaining_messages -= 1
    return events


def _remote_list_targets_sync(handle: RemoteHandle) -> List[Dict[str, Any]]:
    rd = _get_rd()
    url = _remote_url(handle.host, handle.port)
    next_ident = 0
    seen: set[int] = set()
    discovered: List[Dict[str, Any]] = []
    for _ in range(64):
        ident = int(rd.EnumerateRemoteTargets(url, int(next_ident)) or 0)
        if ident <= 0 or ident in seen:
            break
        seen.add(ident)
        target_id = str(ident)
        try:
            control = _create_target_control_sync(handle, ident)
            summary = _target_summary_from_control(handle, ident, control)
            _drain_target_control_messages_sync(handle, control, target_id=target_id, max_messages=4, deadline_s=0.0)
            discovered.append(dict(handle.known_targets.get(target_id) or summary))
        except Exception as exc:
            errored = {
                "target_id": target_id,
                "ident": ident,
                "connected": False,
                "error": str(exc),
            }
            handle.known_targets[target_id] = errored
            discovered.append(errored)
        next_ident = ident
    handle.last_target_scan_ms = _now_ms()
    if discovered:
        return sorted(discovered, key=lambda item: int(item.get("ident") or 0))
    cached = [dict(item) for item in handle.known_targets.values()]
    return sorted(cached, key=lambda item: int(item.get("ident") or 0))


def _resolve_remote_target_ident_sync(handle: RemoteHandle, target_id: str = "") -> Tuple[int, Dict[str, Any]]:
    requested_ident = _remote_target_ident(target_id)
    if requested_ident > 0:
        summary = dict(handle.known_targets.get(str(requested_ident)) or {})
        if not summary:
            _remote_list_targets_sync(handle)
            summary = dict(handle.known_targets.get(str(requested_ident)) or {})
        if not summary:
            raise RuntimeToolError(
                f"Unknown target_id: {target_id}",
                details={"remote_id": handle.remote_id, "target_id": target_id},
            )
        return requested_ident, summary

    targets = _remote_list_targets_sync(handle)
    if len(targets) == 1:
        summary = dict(targets[0])
        return int(summary.get("ident") or 0), summary
    if not targets:
        raise RuntimeToolError(
            "No remote targets are currently available",
            details={"remote_id": handle.remote_id, "endpoint": _remote_url(handle.host, handle.port)},
        )
    raise RuntimeToolError(
        "target_id is required when multiple remote targets are available",
        details={
            "remote_id": handle.remote_id,
            "available_target_ids": [str(item.get("target_id") or "") for item in targets],
        },
    )


def _capture_records_for_target(handle: RemoteHandle, target_id: str = "") -> List[Dict[str, Any]]:
    records = [dict(item) for item in handle.known_captures.values()]
    if target_id:
        records = [item for item in records if str(item.get("target_id") or "") == str(target_id or "")]
    return sorted(
        records,
        key=lambda item: (int(item.get("timestamp") or 0), int(item.get("capture_id") or 0)),
        reverse=True,
    )


def _poll_captures_for_target_sync(
    handle: RemoteHandle,
    *,
    target_id: str,
    deadline_s: float = 3.0,
    max_messages: int = 24,
) -> List[Dict[str, Any]]:
    ident = _remote_target_ident(target_id)
    if ident <= 0:
        return []
    control = _create_target_control_sync(handle, ident)
    _drain_target_control_messages_sync(
        handle,
        control,
        target_id=str(target_id or ""),
        max_messages=max_messages,
        deadline_s=deadline_s,
    )
    return _capture_records_for_target(handle, target_id=str(target_id or ""))


def _launch_remote_app_sync(
    handle: RemoteHandle,
    *,
    exe_path: str,
    working_dir: str,
    cmdline: str,
    env: Dict[str, Any],
    capture_options: Dict[str, Any],
) -> Dict[str, Any]:
    merged_capture_options = {**dict(handle.default_capture_options or {}), **dict(capture_options or {})}
    capture_options_obj, applied_capture_options = _capture_options_from_dict(merged_capture_options)
    env_modifications = _environment_modifications_from_dict(env)
    execute_result = handle.remote_server.ExecuteAndInject(
        str(exe_path or ""),
        str(working_dir or ""),
        str(cmdline or ""),
        env_modifications,
        capture_options_obj,
    )
    status = getattr(execute_result, "result", None)
    status_ok = False
    if status is not None:
        ok_method = getattr(status, "OK", None)
        if callable(ok_method):
            try:
                status_ok = bool(ok_method())
            except Exception:
                status_ok = _status_ok(status)
        else:
            status_ok = _status_ok(status)
    if status is not None and not status_ok:
        raise RuntimeToolError(
            f"ExecuteAndInject({exe_path}) failed: {_status_text(status)}",
            details=build_renderdoc_error_details(
                status,
                operation=f"ExecuteAndInject({exe_path})",
                source_layer="renderdoc_status",
                backend_type="remote",
                capture_context={"remote_id": handle.remote_id, "endpoint": _remote_url(handle.host, handle.port)},
                classification="remote_endpoint",
                fix_hint="Verify the remote endpoint can launch and inject the target application.",
            ),
        )
    ident = int(getattr(execute_result, "ident", 0) or 0)
    if ident <= 0:
        raise RuntimeToolError(
            (
                f"ExecuteAndInject({exe_path}) did not return a target ident"
                + (f": {_status_text(status)}" if status is not None else "")
            ),
            details={
                "remote_id": handle.remote_id,
                "endpoint": _remote_url(handle.host, handle.port),
                "renderdoc_status": {
                    "status_text": _status_text(status) if status is not None else "",
                    "status_ok": status_ok,
                },
            },
        )
    control = _create_target_control_sync(handle, ident, force_connection=False)
    target = _target_summary_from_control(handle, ident, control)
    _drain_target_control_messages_sync(handle, control, target_id=str(ident), max_messages=8, deadline_s=0.5)
    target = dict(handle.known_targets.get(str(ident)) or target)
    target["capture_options"] = applied_capture_options
    return target


def _trigger_remote_capture_sync(
    handle: RemoteHandle,
    *,
    target_id: str,
    num_frames: int,
    capture_delay_ms: int = 0,
    queued: bool = False,
) -> Dict[str, Any]:
    ident, target = _resolve_remote_target_ident_sync(handle, target_id)
    control = _create_target_control_sync(handle, ident, force_connection=False)
    if capture_delay_ms > 0:
        time.sleep(max(0.0, float(capture_delay_ms) / 1000.0))
    if queued:
        control.QueueCapture(0, max(1, int(num_frames or 1)))
    else:
        control.TriggerCapture(max(1, int(num_frames or 1)))
    captures = _poll_captures_for_target_sync(
        handle,
        target_id=str(ident),
        deadline_s=5.0 if not queued else 2.0,
        max_messages=32,
    )
    return {
        "target": dict(handle.known_targets.get(str(ident)) or target),
        "captures": captures,
        "queue_status": {
            "queued": bool(queued),
            "target_id": str(ident),
            "num_frames": max(1, int(num_frames or 1)),
        },
    }

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

    return tools_root() / "spec" / "tool_catalog.json"


def _load_tool_catalog() -> List[Dict[str, Any]]:
    path = _tool_catalog_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    tools = list(data.get("tools", []))
    declared_count = int(data.get("tool_count") or len(tools))
    if len(tools) != declared_count:
        raise RuntimeError(f"Catalog tool_count mismatch: declared {declared_count}, got {len(tools)} entries")
    names = [str(t.get("name", "")).strip() for t in tools]
    if len(set(names)) != len(names):
        raise RuntimeError("Catalog contains duplicate tool names")
    if any(not name.startswith("rd.") for name in names):
        raise RuntimeError("Catalog contains invalid tool name prefixes")
    return tools


def _resource_keys(resource_id: Any) -> List[str]:
    keys = [str(resource_id)]
    try:
        keys.append(str(int(resource_id)))
    except Exception:
        pass
    return keys


def _is_null_resource_id(resource_id: Any) -> bool:
    rd = _get_rd()
    try:
        return resource_id == rd.ResourceId()
    except Exception:
        return resource_id is None


def _parse_remote_endpoint(endpoint: str) -> Tuple[str, int]:
    text = str(endpoint or "").strip()
    if not text:
        return "", 0
    if ":" not in text:
        return text, 0
    host, raw_port = text.rsplit(":", 1)
    return host.strip(), _as_int(raw_port, 0)


def _remote_session_metadata(
    handle: RemoteHandle,
    *,
    remote_id: str,
    endpoint: str,
) -> Dict[str, Any]:
    requested_host = str(handle.requested_host or handle.host or "").strip()
    requested_port = int(handle.requested_port or handle.port or 0)
    bootstrap = dict(handle.bootstrap or {})
    return {
        "transport": str(handle.transport or "renderdoc"),
        "host": str(handle.host or "").strip(),
        "port": int(handle.port or 0),
        "endpoint": str(endpoint or _remote_url(handle.host, handle.port)),
        "origin_remote_id": str(remote_id or handle.remote_id or "").strip(),
        "ownership_state": "session_owned",
        "device_serial": str(
            handle.device_serial
            or bootstrap.get("device_serial")
            or ""
        ).strip(),
        "requested": {
            "host": requested_host,
            "port": requested_port,
        },
        "options": {
            "install_apk": _as_bool(bootstrap.get("installed_apk"), True),
            "push_config": _as_bool(bootstrap.get("pushed_config"), True),
            "local_port": int(handle.port or 0),
            "remote_port": _as_int(bootstrap.get("remote_port"), requested_port),
        },
        "bootstrap": {
            "package_name": str(bootstrap.get("package_name") or "").strip(),
            "activity_name": str(bootstrap.get("activity_name") or "").strip(),
            "abi": str(bootstrap.get("abi") or "").strip(),
            "remote_port": _as_int(bootstrap.get("remote_port"), requested_port),
            "config_remote_path": str(bootstrap.get("config_remote_path") or "").strip(),
        },
    }


def _session_record_remote_metadata(session_record: Dict[str, Any]) -> Dict[str, Any]:
    remote = session_record.get("remote")
    return dict(remote or {}) if isinstance(remote, dict) else {}


def _session_replace_capability(session_id: str, controller: Any) -> Tuple[bool, str]:
    try:
        state = _session_manager.get_state(session_id)
    except Exception:
        state = None
    methods = ("BuildTargetShader", "ReplaceResource", "RemoveReplacement", "FreeTargetResource")
    missing = [name for name in methods if not hasattr(controller, name)]
    supported = not missing
    reason = (
        "Replay controller exposes runtime shader replacement APIs."
        if supported
        else f"Replay controller is missing runtime shader replacement APIs: {', '.join(missing)}"
    )
    if state is not None:
        state.capabilities.patch_supported = supported
    return supported, reason


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


def _build_action_index(actions: Sequence[Any]) -> Tuple[List[Any], Dict[int, Any]]:
    flat = _flatten_actions(actions)
    by_event: Dict[int, Any] = {}
    for action in flat:
        event_id = int(getattr(action, "eventId", 0))
        if event_id > 0 and event_id not in by_event:
            by_event[event_id] = action
    return flat, by_event


async def _load_action_index(session_id: str, *, controller: Optional[Any] = None) -> Tuple[Sequence[Any], List[Any], Dict[int, Any]]:
    if controller is None:
        controller = await _get_controller(session_id)
    roots = await _offload(controller.GetRootActions)
    flat, by_event = _build_action_index(roots)
    return roots, flat, by_event


def _pick_default_event_id(actions: Sequence[Any]) -> int:
    flat, _ = _build_action_index(actions)
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


async def _store_text_artifact_payload(
    text: str,
    *,
    stem: str,
    suffix: str,
    output_dir: str = "",
    title: str = "",
    mime: str = "text/plain",
) -> Dict[str, Any]:
    if not text:
        return {}
    saved_path = ""
    if output_dir:
        out_dir = Path(str(output_dir))
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{_safe_name_token(stem)}{suffix}"
        out_path.write_text(text, encoding="utf-8")
        saved_path = str(out_path)
    artifact_path = ""
    if _artifact_store is not None:
        artifact = await _artifact_store.store(
            text.encode("utf-8"),
            mime=mime,
            suffix=suffix,
        )
        artifact_path = str(_artifact_path(artifact) or "")
    payload: Dict[str, Any] = {}
    if title:
        payload["title"] = str(title)
    if artifact_path:
        payload["artifact_path"] = artifact_path
    if saved_path:
        payload["saved_path"] = saved_path
    elif artifact_path:
        payload["saved_path"] = artifact_path
    if payload:
        payload["type"] = "saved_path"
    return payload


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


async def _ensure_live_session(session_id: str) -> ReplayHandle:
    assert _session_manager is not None
    replay = _runtime.replays.get(str(session_id))
    if replay is not None:
        try:
            _session_manager.get_controller(str(session_id))
            _session_manager.get_output(str(session_id))
            return replay
        except SessionError:
            pass
    await _recover_single_session_from_state(_runtime_context_id(), str(session_id))
    replay = _runtime.replays.get(str(session_id))
    if replay is None:
        raise CoreError(
            code="session_resume_failed",
            message=f"Session could not be resumed: {session_id}",
            category="runtime",
            details={"session_id": str(session_id)},
        )
    return replay


async def _get_controller(session_id: str) -> Any:
    assert _session_manager is not None
    await _ensure_live_session(session_id)
    return _session_manager.get_controller(session_id)


async def _get_output(session_id: str) -> Any:
    assert _session_manager is not None
    await _ensure_live_session(session_id)
    return _session_manager.get_output(session_id)


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


def _raise_event_not_found(session_id: str, event_id: int) -> None:
    raise CoreError(
        code="event_not_found",
        message=f"Event not found: {int(event_id)}",
        category="not_found",
        details={
            "session_id": str(session_id or ""),
            "event_id": int(event_id),
        },
    )


def _require_action_event(session_id: str, event_id: int, by_event: Dict[int, Any]) -> int:
    resolved = int(event_id)
    if resolved <= 0 or resolved not in by_event:
        _raise_event_not_found(session_id, resolved)
    return resolved


def _store_active_event(session_id: str, event_id: int, *, context_id: Optional[str] = None) -> None:
    resolved = int(event_id or 0)
    if session_id in _runtime.replays:
        _runtime.replays[session_id].active_event_id = resolved
    _set_context_active_event(session_id, resolved, context_id=context_id)


def _capture_dependent_session_ids(capture_file_id: str) -> List[str]:
    wanted = str(capture_file_id or "")
    dependent = [
        str(session_id)
        for session_id, handle in _runtime.replays.items()
        if str(handle.capture_file_id or "") == wanted
    ]
    dependent.sort()
    return dependent


async def _ensure_event(session_id: str, event_id: Optional[int]) -> int:
    controller = await _get_controller(session_id)
    roots, _, by_event = await _load_action_index(session_id, controller=controller)
    should_repair_state = False
    if event_id is None:
        active_event = _active_event(session_id)
        if active_event > 0 and active_event in by_event:
            resolved_event = active_event
        else:
            resolved_event = _pick_default_event_id(roots)
            should_repair_state = active_event != resolved_event
    else:
        resolved_event = _require_action_event(session_id, int(event_id), by_event)
    if resolved_event > 0:
        await _offload(controller.SetFrameEvent, resolved_event, True)
        _store_active_event(session_id, resolved_event)
    elif should_repair_state:
        _store_active_event(session_id, resolved_event)
    return resolved_event


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


async def _resolve_target_texture_for_event(
    session_id: str,
    target: Optional[Dict[str, Any]],
    *,
    event_id: Optional[int] = None,
) -> Tuple[Any, Optional[Any]]:
    parsed = target or {}
    explicit_texture = parsed.get("texture_id") or parsed.get("textureId")
    if explicit_texture is not None and str(explicit_texture).strip():
        return await _get_texture_descriptor(
            session_id,
            explicit_texture,
            event_id=event_id,
        )

    raw_rt_index = parsed.get("rt_index")
    if raw_rt_index is not None:
        outputs = await _output_target_resource_ids(session_id, event_id)
        rt_index = _as_int(raw_rt_index, -1)
        if 0 <= rt_index < len(outputs):
            rid, _ = outputs[rt_index]
            return await _get_texture_descriptor(
                session_id,
                rid,
                event_id=event_id,
            )
        raise ValueError(f"Render target index out of range: {raw_rt_index}")

    return await _get_texture_descriptor(session_id, None, event_id=event_id)


async def _configure_texture_output_for_target(
    session_id: str,
    target: Optional[Dict[str, Any]],
    *,
    event_id: Optional[int] = None,
    sample_override: Optional[int] = None,
) -> Tuple[Any, Optional[Any], Any]:
    assert _session_manager is not None
    rd = _get_rd()
    parsed = target or {}
    rid, texture_desc = await _resolve_target_texture_for_event(
        session_id,
        parsed,
        event_id=event_id,
    )

    sub_dict = _as_dict(parsed.get("subresource"), default={})
    raw_sample = sub_dict.get("sample", parsed.get("sample"))
    sample_value = sample_override if sample_override is not None else (
        _as_int(raw_sample, 0) if raw_sample is not None else 0
    )

    sub = rd.Subresource()
    sub.mip = _as_int(sub_dict.get("mip", parsed.get("mip")), 0)
    sub.slice = _as_int(sub_dict.get("slice", parsed.get("slice")), 0)
    sub.sample = int(sample_value)

    display = rd.TextureDisplay()
    display.resourceId = rid
    display.subresource = sub
    display.typeCast = rd.CompType.Typeless
    display.overlay = rd.DebugOverlay.NoOverlay

    output = await _get_output(session_id)
    await _offload(output.SetTextureDisplay, display)
    try:
        await _offload(output.Display)
    except Exception:
        pass
    return rid, texture_desc, sub


def _subresource_to_dict(sub: Any) -> Dict[str, int]:
    return {
        "mip": int(getattr(sub, "mip", 0)),
        "slice": int(getattr(sub, "slice", 0)),
        "sample": int(getattr(sub, "sample", 0)),
    }


async def _refresh_pixel_context(session_id: str, x: int, y: int) -> None:
    output = await _get_output(session_id)
    try:
        await _offload(output.SetPixelContextLocation, x, y)
    except Exception:
        pass
    try:
        await _offload(output.Display)
    except Exception:
        pass


async def _pixel_history_raw(controller: Any, resource_id: Any, x: int, y: int, subresource: Any) -> List[Any]:
    rd = _get_rd()
    try:
        history_raw = await _offload(
            controller.PixelHistory,
            resource_id,
            x,
            y,
            subresource,
            rd.CompType.Typeless,
        )
    except Exception:
        history_raw = await _offload(
            controller.PixelHistory,
            resource_id,
            x,
            y,
            subresource,
        )
    return list(history_raw or [])


def _pixel_history_timeout_message(timeout_s: float) -> str:
    return f"PixelHistory timed out after {timeout_s:.1f}s"


async def _pixel_history_raw_with_timeout(
    controller: Any,
    resource_id: Any,
    x: int,
    y: int,
    subresource: Any,
    *,
    timeout_s: Optional[float] = None,
) -> List[Any]:
    effective_timeout = float(PIXEL_HISTORY_TIMEOUT_S if timeout_s is None else timeout_s)
    return await asyncio.wait_for(
        _pixel_history_raw(controller, resource_id, x, y, subresource),
        timeout=effective_timeout,
    )


def _pixel_history_item_payload(item: Any) -> Dict[str, Any]:
    passed = bool(item.Passed()) if hasattr(item, "Passed") else False
    flags: List[str] = []
    if passed:
        flags.append("passed")
    if bool(getattr(item, "depthTestFailed", False)):
        flags.append("depth_test_failed")
    if bool(getattr(item, "stencilTestFailed", False)):
        flags.append("stencil_test_failed")
    if bool(getattr(item, "shaderDiscarded", False)):
        flags.append("shader_discarded")
    if bool(getattr(item, "unboundPS", False)):
        flags.append("unbound_ps")
    if bool(getattr(item, "sampleMasked", False)):
        flags.append("sample_masked")
    if bool(getattr(item, "scissorClipped", False)):
        flags.append("scissor_clipped")
    if bool(getattr(item, "viewClipped", False)):
        flags.append("view_clipped")
    if bool(getattr(item, "backfaceCulled", False)):
        flags.append("backface_culled")
    if bool(getattr(item, "directShaderWrite", False)):
        flags.append("direct_shader_write")
    return {
        "event_id": int(getattr(item, "eventId", 0)),
        "primitive_id": int(getattr(item, "primitiveID", -1)),
        "frag_index": int(getattr(item, "fragIndex", -1)),
        "passed": passed,
        "depth_test_failed": bool(getattr(item, "depthTestFailed", False)),
        "stencil_test_failed": bool(getattr(item, "stencilTestFailed", False)),
        "shader_discarded": bool(getattr(item, "shaderDiscarded", False)),
        "unbound_ps": bool(getattr(item, "unboundPS", False)),
        "sample_masked": bool(getattr(item, "sampleMasked", False)),
        "scissor_clipped": bool(getattr(item, "scissorClipped", False)),
        "view_clipped": bool(getattr(item, "viewClipped", False)),
        "backface_culled": bool(getattr(item, "backfaceCulled", False)),
        "direct_shader_write": bool(getattr(item, "directShaderWrite", False)),
        "flags": ",".join(flags) if flags else "unknown",
    }


def _pixel_history_summary(items: Sequence[Dict[str, Any]], event_id: int) -> Dict[str, Any]:
    matched = [item for item in items if int(item.get("event_id") or 0) == int(event_id)]
    passed = [item for item in matched if bool(item.get("passed"))]
    viable = [
        item
        for item in passed
        if not bool(item.get("shader_discarded")) and not bool(item.get("unbound_ps"))
    ]
    primitive_ids = [
        int(item.get("primitive_id"))
        for item in viable
        if isinstance(item.get("primitive_id"), int) and int(item.get("primitive_id")) >= 0
    ]
    return {
        "hit_count": len(items),
        "matched_event_hit_count": len(matched),
        "passed_hit_count": len(passed),
        "viable_hit_count": len(viable),
        "primitive_ids": primitive_ids,
    }


def _build_synthetic_debug_states(
    resolved_context: Dict[str, Any],
    pixel_history_summary: Dict[str, Any],
) -> List[Any]:
    variables = [
        SimpleNamespace(name="event_id", value=resolved_context.get("event_id")),
        SimpleNamespace(name="primitive", value=resolved_context.get("primitive")),
        SimpleNamespace(name="x", value=resolved_context.get("x")),
        SimpleNamespace(name="y", value=resolved_context.get("y")),
        SimpleNamespace(name="pixel_history_hits", value=pixel_history_summary.get("hit_count", 0)),
    ]
    return [
        SimpleNamespace(
            stepIndex=index,
            changes=variables,
            callstack=[SimpleNamespace(function="main", file="", line=0, address=str(index))],
        )
        for index in range(3)
    ]


async def _pipeline_snapshot(session_id: str, event_id: Optional[int] = None) -> Any:
    assert _pipeline_service is not None
    evt = await _ensure_event(session_id, event_id)
    return await _pipeline_service.snapshot_pipeline(
        session_id=session_id,
        event_id=evt,
        session_manager=_session_manager,
    )


def _recovery_payload(ok: bool, *, duration_ms: int, message: str = "", code: str = "") -> Dict[str, Any]:
    return {
        "ok": ok,
        "meta": {"duration_ms": int(duration_ms)},
        "error": ({"code": str(code or "runtime_error"), "message": str(message)} if (not ok and message) else {}),
    }


async def _restore_capture_handle_from_state(
    context_id: str,
    capture_file_id: str,
    record: Dict[str, Any],
) -> Optional[CaptureFileHandle]:
    handle = _runtime.captures.get(str(capture_file_id))
    if handle is not None:
        return handle
    file_path = str(record.get("file_path") or "").strip()
    meta = _capture_file_metadata(file_path)
    if not meta.get("file_path") or not Path(str(meta.get("file_path"))).is_file():
        return None
    handle = CaptureFileHandle(
        capture_file_id=str(capture_file_id),
        file_path=str(meta.get("file_path") or file_path),
        read_only=bool(record.get("read_only", True)),
        driver=str(record.get("driver") or ""),
    )
    _runtime.captures[str(capture_file_id)] = handle
    return handle


async def _restore_remote_handle_from_session_record(
    session_id: str,
    session_record: Dict[str, Any],
) -> RemoteHandle:
    remote_record = _session_record_remote_metadata(session_record)
    transport = str(remote_record.get("transport") or "renderdoc").strip() or "renderdoc"
    endpoint = str(remote_record.get("endpoint") or "").strip()
    endpoint_host = str(remote_record.get("host") or "").strip()
    endpoint_port = _as_int(remote_record.get("port"), 0)
    origin_remote_id = str(remote_record.get("origin_remote_id") or "").strip()
    requested = _as_dict(remote_record.get("requested"), default={})
    requested_host = str(requested.get("host") or endpoint_host or "127.0.0.1").strip() or "127.0.0.1"
    requested_port = _as_int(
        requested.get("port"),
        _as_int(_as_dict(remote_record.get("bootstrap"), default={}).get("remote_port"), endpoint_port or 38920),
    )
    device_serial = str(remote_record.get("device_serial") or "").strip()
    options = _as_dict(remote_record.get("options"), default={})
    bootstrap_result = None
    bootstrap_detail = _as_dict(remote_record.get("bootstrap"), default={})

    if origin_remote_id and origin_remote_id in _runtime.remotes:
        live_handle = _runtime.remotes.get(origin_remote_id)
        if live_handle is not None and live_handle.connected:
            live_handle.requested_host = requested_host
            live_handle.requested_port = requested_port
            if device_serial:
                live_handle.device_serial = device_serial
            if bootstrap_detail:
                live_handle.bootstrap = dict(bootstrap_detail)
            return live_handle

    if transport == "adb_android":
        bootstrap_result = await _offload(
            bootstrap_android_remote,
            remote_port=_as_int(
                options.get("remote_port"),
                _as_int(bootstrap_detail.get("remote_port"), requested_port or 38920),
            ),
            options=AndroidBootstrapOptions(
                device_serial=device_serial,
                local_port=_as_int(options.get("local_port"), 0),
                install_apk=_as_bool(options.get("install_apk"), True),
                push_config=_as_bool(options.get("push_config"), True),
            ),
        )
        bootstrap_detail = describe_android_remote(bootstrap_result)
        endpoint_host = str(bootstrap_result.host)
        endpoint_port = int(bootstrap_result.port)
        requested_host = "127.0.0.1"
        requested_port = _as_int(bootstrap_detail.get("remote_port"), requested_port or 38920)
    else:
        if (not endpoint_host or endpoint_port <= 0) and endpoint:
            endpoint_host, endpoint_port = _parse_remote_endpoint(endpoint)
        if not endpoint_host:
            endpoint_host = requested_host
        if endpoint_port <= 0:
            endpoint_port = requested_port

    if not endpoint_host or endpoint_port <= 0:
        if bootstrap_result is not None:
            try:
                await _offload(cleanup_android_remote, bootstrap_result)
            except Exception:
                pass
        raise RuntimeToolError(
            "Remote recovery metadata is incomplete",
            details={
                "session_id": session_id,
                "remote": remote_record,
            },
        )

    url = _remote_url(endpoint_host, endpoint_port)
    try:
        await _offload(_wait_for_remote_endpoint, url, remote_connect_timeout_ms({}))
        remote_server = await _offload(_create_remote_server_connection, url)
        ping_status = await _offload(remote_server.Ping)
        if not _status_ok(ping_status):
            raise RuntimeToolError(
                f"RemoteServer.Ping({url}) failed: {_status_text(ping_status)}",
                details=build_renderdoc_error_details(
                    ping_status,
                    operation=f"RemoteServer.Ping({url})",
                    source_layer="renderdoc_status",
                    backend_type="remote",
                    capture_context={"session_id": session_id, "endpoint": url},
                    classification="remote_endpoint",
                    fix_hint="Reconnect or re-bootstrap the remote endpoint before retrying recovery.",
                ),
            )
        server_info = await _offload(
            _collect_remote_server_info,
            remote_server,
            host=endpoint_host,
            port=endpoint_port,
            transport=transport,
            bootstrap=bootstrap_detail,
        )
    except Exception:
        if bootstrap_result is not None:
            try:
                await _offload(cleanup_android_remote, bootstrap_result)
            except Exception:
                pass
        raise

    return RemoteHandle(
        remote_id=origin_remote_id or _new_id("remote"),
        host=endpoint_host,
        port=endpoint_port,
        connected=True,
        transport=transport,
        remote_server=remote_server,
        server_info=server_info,
        bootstrap=bootstrap_detail,
        bootstrap_result=bootstrap_result,
        leased_session_id="",
        leased_session_ids=[],
        requested_host=requested_host,
        requested_port=requested_port,
        device_serial=device_serial,
        detail={
            "connected": True,
            "transport": transport,
            "endpoint": url,
            "requires_remote_device": transport == "adb_android",
            "bootstrap": dict(bootstrap_detail) if bootstrap_detail else {},
        },
    )


async def _recover_single_session_from_state(
    context_id: str,
    session_id: str,
    *,
    trace_id: str = "",
) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id)
    state = _context_state(ctx)
    sessions = state.setdefault("sessions", {})
    captures = state.setdefault("captures", {})
    session_record = dict(sessions.get(str(session_id)) or {})
    if not session_record:
        raise CoreError(
            code="session_not_found",
            message=f"Unknown session_id: {session_id}",
            category="not_found",
            details={"session_id": str(session_id)},
        )
    if str(session_record.get("backend_type") or "local") == "remote" and not _session_record_remote_metadata(session_record):
        raise RuntimeToolError(
            "Remote session recovery metadata is missing",
            details={"session_id": str(session_id)},
        )

    try:
        existing = _session_manager.get_state(str(session_id))
    except Exception:
        existing = None
    if existing is not None:
        try:
            await _session_manager.close_session(str(session_id))
        except Exception:
            pass

    capture_file_id = str(session_record.get("capture_file_id") or "")
    capture_record = captures.get(capture_file_id) if capture_file_id else None
    capture_path = str(session_record.get("rdc_path") or (capture_record or {}).get("file_path") or "").strip()
    capture_meta = _capture_file_metadata(capture_path)
    if not capture_meta.get("file_path") or not Path(str(capture_meta.get("file_path"))).is_file():
        raise RuntimeToolError(
            "capture file missing",
            details={"session_id": str(session_id), "capture_path": capture_path},
        )

    if not capture_file_id:
        capture_file_id = _new_id("capf")
        session_record["capture_file_id"] = capture_file_id

    capture_handle = await _restore_capture_handle_from_state(
        ctx,
        capture_file_id,
        {
            "capture_file_id": capture_file_id,
            "file_path": str(capture_meta.get("file_path") or capture_path),
            "read_only": True,
            "driver": str((capture_record or {}).get("driver") or ""),
        },
    )
    if capture_handle is None:
        raise RuntimeToolError(
            "capture handle restore failed",
            details={"session_id": str(session_id), "capture_file_id": capture_file_id},
        )

    backend_type = str(session_record.get("backend_type") or "local")
    backend_config: Dict[str, Any] = {"type": "local"}
    remote_handle_for_session: Optional[RemoteHandle] = None
    remote_endpoint = ""
    if backend_type == "remote":
        remote_handle_for_session = await _restore_remote_handle_from_session_record(
            str(session_id),
            session_record,
        )
        remote_endpoint = str(remote_handle_for_session.detail.get("endpoint") or _remote_url(remote_handle_for_session.host, remote_handle_for_session.port))
        _runtime.remotes[str(remote_handle_for_session.remote_id)] = remote_handle_for_session
        backend_config = {
            "type": "remote",
            "host": remote_handle_for_session.host,
            "port": remote_handle_for_session.port,
            "transport": remote_handle_for_session.transport,
            "remote_id": remote_handle_for_session.remote_id,
            "remote_server": remote_handle_for_session.remote_server,
            "close_remote_server_on_cleanup": False,
        }

    if trace_id:
        _record_operation_stage(
            ctx,
            trace_id=trace_id,
            stage="resume_session",
            message=f"Reopening {backend_type} session {session_id}",
            details={"session_id": session_id, "capture_file_id": capture_file_id, "backend_type": backend_type},
        )

    try:
        await _session_manager.create_session(
            backend_config=backend_config,
            replay_config={},
            preferred_session_id=str(session_id),
        )
        await _session_manager.open_capture(str(session_id), str(capture_handle.file_path))
        controller = _session_manager.get_controller(str(session_id))
        roots = await _offload(controller.GetRootActions)
        _, by_event = _build_action_index(roots)
        desired_event_id = int(session_record.get("active_event_id") or 0)
        if desired_event_id <= 0 or desired_event_id not in by_event:
            desired_event_id = _pick_default_event_id(roots)
        if desired_event_id > 0:
            await _offload(controller.SetFrameEvent, desired_event_id, True)
        replay = ReplayHandle(
            session_id=str(session_id),
            capture_file_id=str(capture_file_id),
            frame_index=int(session_record.get("frame_index") or 0),
            active_event_id=int(desired_event_id or 0),
        )
        _runtime.replays[str(session_id)] = replay
        captures[str(capture_file_id)] = _capture_record_from_runtime(str(capture_file_id))
        remote_metadata: Dict[str, Any] = {}
        if remote_handle_for_session is not None:
            origin_remote_id = str(_session_record_remote_metadata(session_record).get("origin_remote_id") or remote_handle_for_session.remote_id or _new_id("remote"))
            remote_handle_for_session.remote_id = origin_remote_id
            _runtime.remotes[origin_remote_id] = remote_handle_for_session
            _register_remote_session_lease(str(session_id), remote_handle_for_session, context_id=ctx)
            remote_metadata = _remote_session_metadata(
                remote_handle_for_session,
                remote_id=origin_remote_id,
                endpoint=remote_endpoint,
            )
        session_record["rdc_path"] = str(capture_handle.file_path)
        session_record["file_fingerprint"] = str(capture_meta.get("file_fingerprint") or "")
        session_record["file_size_bytes"] = int(capture_meta.get("file_size_bytes") or 0)
        session_record["active_event_id"] = int(desired_event_id or 0)
        session_record["state"] = "active"
        session_record["is_live"] = True
        session_record["last_error"] = ""
        session_record["updated_at_ms"] = _now_ms()
        if remote_metadata:
            session_record["remote"] = remote_metadata
        session_record["recovery"] = {
            **dict(session_record.get("recovery") or {}),
            "status": "recovered",
            "last_attempt_ms": _now_ms(),
            "last_success_ms": _now_ms(),
            "attempt_count": int(dict(session_record.get("recovery") or {}).get("attempt_count") or 0) + 1,
            "last_error": "",
        }
        sessions[str(session_id)] = session_record
        state["captures"] = captures
        state["sessions"] = sessions
        state = _select_session_from_state(state, session_id=str(session_id), capture_file_id=str(capture_file_id))
        _store_context_state(state, ctx)
        _sync_context_metrics(ctx)
        _set_context_runtime_session(
            str(session_id),
            capture_file_id=str(capture_file_id),
            backend_type=backend_type,
            frame_index=int(session_record.get("frame_index") or 0),
            active_event_id=int(desired_event_id or 0),
            remote_metadata=session_record.get("remote") if isinstance(session_record.get("remote"), dict) else {},
            context_id=ctx,
        )
        return sessions[str(session_id)]
    except Exception as exc:
        try:
            await _session_manager.close_session(str(session_id))
        except Exception:
            pass
        _runtime.replays.pop(str(session_id), None)
        if remote_handle_for_session is not None:
            _release_remote_session_lease(str(session_id), context_id=ctx)
        session_record["state"] = "degraded"
        session_record["is_live"] = False
        session_record["last_error"] = str(exc)
        session_record["updated_at_ms"] = _now_ms()
        session_record["recovery"] = {
            **dict(session_record.get("recovery") or {}),
            "status": "degraded",
            "last_attempt_ms": _now_ms(),
            "attempt_count": int(dict(session_record.get("recovery") or {}).get("attempt_count") or 0) + 1,
            "last_error": str(exc),
        }
        sessions[str(session_id)] = session_record
        state["sessions"] = sessions
        _store_context_state(state, ctx)
        _sync_context_metrics(ctx)
        _sync_context_snapshot_from_state(ctx)
        raise


async def _maybe_refresh_remote_session_after_revert(
    session_id: str,
    *,
    remaining_replacements: Sequence[Dict[str, Any]],
) -> None:
    if remaining_replacements:
        return
    ctx = _runtime_context_id()
    state = _context_state(ctx)
    sessions = state.setdefault("sessions", {})
    session_record = dict(sessions.get(str(session_id)) or {})
    if not session_record:
        return
    if str(session_record.get("backend_type") or "local") != "remote":
        return

    try:
        await _session_manager.close_session(str(session_id))
    except Exception:
        logger.debug(
            "Best-effort close_session during replacement revert refresh failed",
            exc_info=True,
        )

    _runtime.replays.pop(str(session_id), None)
    owned_handle = _release_remote_session_lease(str(session_id), context_id=ctx)
    remote_meta = _session_record_remote_metadata(session_record)
    origin_remote_id = str(
        remote_meta.get("origin_remote_id")
        or getattr(owned_handle, "remote_id", "")
        or "",
    ).strip()

    live_handle = _runtime.remotes.pop(origin_remote_id, None) if origin_remote_id else None
    if origin_remote_id:
        _clear_context_remote_live(origin_remote_id, context_id=ctx)
    if live_handle is None:
        live_handle = owned_handle
    if live_handle is not None:
        try:
            await _offload(_disconnect_remote_handle_sync, live_handle)
        except Exception:
            logger.debug(
                "Best-effort remote disconnect during replacement revert refresh failed",
                exc_info=True,
            )

    await _recover_single_session_from_state(ctx, str(session_id))


async def _recover_context_sessions(context_id: str) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id)
    state = _context_state(ctx)
    start_ms = _now_ms()
    trace_id = f"rcv_{ctx}_{start_ms}"
    recovered: list[str] = []
    degraded: list[str] = []
    _record_operation_start(ctx, trace_id=trace_id, operation="rd.session.resume", transport="recovery", args={"context_id": ctx})
    _record_operation_stage(ctx, trace_id=trace_id, stage="scan", message="Scanning persisted local sessions for recovery")
    recovery = dict(state.get("recovery") or {})
    recovery["status"] = "scanning"
    recovery["last_scan_ms"] = start_ms
    recovery["last_attempt_ms"] = start_ms
    recovery["attempt_count"] = int(recovery.get("attempt_count") or 0) + 1
    recovery["last_error"] = ""
    metrics = dict(state.get("metrics") or {})
    metrics["recovery_attempt_count"] = int(metrics.get("recovery_attempt_count") or 0) + 1
    state["recovery"] = recovery
    state["metrics"] = metrics
    _store_context_state(state, ctx)

    session_limit = int(_runtime_limits().get("max_sessions_per_context", 4) or 4)
    sessions = state.get("sessions", {}) if isinstance(state.get("sessions"), dict) else {}
    captures = state.get("captures", {}) if isinstance(state.get("captures"), dict) else {}
    live_count = len(_runtime.replays)
    for capture_file_id, capture_record in list(captures.items()):
        handle = await _restore_capture_handle_from_state(ctx, str(capture_file_id), dict(capture_record or {}))
        if handle is None:
            capture_record["recovery_status"] = "missing"
            capture_record["last_error"] = "capture file missing"
        else:
            capture_record["recovery_status"] = "ready"
            capture_record["last_error"] = ""
        capture_record["updated_at_ms"] = _now_ms()
        captures[str(capture_file_id)] = capture_record

    for session_id, session_record in list(sessions.items()):
        session_record = dict(session_record or {})
        if live_count >= session_limit and session_id not in _runtime.replays:
            session_record["state"] = "degraded"
            session_record["is_live"] = False
            session_record["last_error"] = "session limit exceeded during recovery"
            session_record["recovery"] = {
                **dict(session_record.get("recovery") or {}),
                "status": "degraded",
                "last_attempt_ms": _now_ms(),
                "attempt_count": int(dict(session_record.get("recovery") or {}).get("attempt_count") or 0) + 1,
                "last_error": "session limit exceeded during recovery",
            }
            sessions[str(session_id)] = session_record
            degraded.append(str(session_id))
            continue
        if session_id in _runtime.replays:
            recovered.append(str(session_id))
            continue
        try:
            updated_record = await _recover_single_session_from_state(
                ctx,
                str(session_id),
                trace_id=trace_id,
            )
            sessions[str(session_id)] = updated_record
            live_count += 1
            recovered.append(str(session_id))
        except Exception as exc:
            sessions[str(session_id)] = dict(state.get("sessions", {}).get(str(session_id)) or session_record)
            degraded.append(str(session_id))

    recovery = dict(state.get("recovery") or {})
    recovery["status"] = "ready"
    recovery["last_scan_ms"] = _now_ms()
    recovery["last_success_ms"] = _now_ms() if recovered else int(recovery.get("last_success_ms") or 0)
    recovery["recovered_session_ids"] = recovered
    recovery["degraded_session_ids"] = degraded
    recovery["last_error"] = "" if not degraded else str((sessions.get(degraded[0]) or {}).get("last_error") or "")
    state["captures"] = captures
    state["sessions"] = sessions
    state["recovery"] = recovery
    metrics = dict(state.get("metrics") or {})
    if recovered:
        metrics["recovery_success_count"] = int(metrics.get("recovery_success_count") or 0) + len(recovered)
    if degraded:
        metrics["recovery_failure_count"] = int(metrics.get("recovery_failure_count") or 0) + len(degraded)
    metrics["last_recovery_ms"] = _now_ms()
    state["metrics"] = metrics
    state = _select_session_from_state(state)
    _store_context_state(state, ctx)
    _sync_context_metrics(ctx)
    _sync_context_snapshot_from_state(ctx)
    duration_ms = _now_ms() - start_ms
    _record_operation_finish(
        ctx,
        trace_id=trace_id,
        payload=_recovery_payload(not degraded, duration_ms=duration_ms, message=recovery.get("last_error") or "", code="recovery_degraded"),
    )
    return _context_state(ctx)


async def ensure_context_ready(context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    _context_state(ctx)
    if ctx in _runtime.hydrated_contexts:
        return _sync_context_metrics(ctx)
    await _recover_context_sessions(ctx)
    _runtime.hydrated_contexts.add(ctx)
    return _context_state(ctx)

async def runtime_startup() -> None:
    global _config, _session_manager, _event_graph_service, _render_service
    global _pipeline_service, _perf_service, _patch_engine
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
    artifact_root = Path(os.environ.get("RDX_ARTIFACT_DIR", str(_config.artifact.store_path))).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    _config.artifact.store_path = artifact_root
    _artifact_store = ArtifactStore(root=artifact_root)

    _session_manager = SessionManager()
    _event_graph_service = EventGraphService()
    _render_service = RenderService()
    _pipeline_service = PipelineService()
    _perf_service = PerfService()
    _patch_engine = PatchEngine()

    _runtime.config = _serialize_runtime_config()
    _runtime.initialized = False
    _runtime.logs.clear()
    _runtime.context_states.clear()
    _runtime.hydrated_contexts.clear()
    _runtime_bootstrapped = True
    _record_log("info", "RDX runtime initialized")


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
        if _patch_engine is not None:
            for session_id in list(_runtime.replays.keys()):
                try:
                    await _patch_engine.revert_all(session_id, _session_manager)
                except Exception:
                    pass
        _runtime.shader_replacements.clear()
        for info in list(_session_manager.list_sessions()):
            try:
                await _session_manager.close_session(info.session_id)
            except Exception:
                pass
    for remote_id in list(_runtime.remotes.keys()):
        handle = _runtime.remotes.pop(remote_id, None)
        if handle is not None:
            try:
                await _offload(_disconnect_remote_handle_sync, handle)
            except Exception:
                pass
    _runtime.session_owned_remotes.clear()
    _runtime.consumed_remotes.clear()
    _runtime.context_snapshots.clear()
    _runtime.context_states.clear()
    _runtime.hydrated_contexts.clear()
    _runtime_bootstrapped = False
    _record_log("info", "RDX runtime shutdown complete")


async def _dispatch_core(action: str, args: Dict[str, Any]) -> str:
    if action == "init":
        global_env = _as_dict(args.get("global_env"), default={})
        _runtime.enable_remote = _as_bool(args.get("enable_remote"), True)
        if global_env:
            _apply_runtime_config(global_env)
        _runtime.initialized = True
        _sync_context_metrics(_runtime_context_id())
        version = await _core_get_version_value()
        capabilities = await _core_capabilities(detail="summary")
        return _ok(api_version=version, capabilities=capabilities)

    if action == "shutdown":
        unique_remote_ids = {
            str(item.remote_id or "")
            for item in list(_runtime.remotes.values()) + list(_runtime.session_owned_remotes.values())
            if item is not None and str(item.remote_id or "").strip()
        }
        released = {
            "sessions": len(_runtime.replays),
            "capture_files": len(_runtime.captures),
            "remote_connections": len(unique_remote_ids),
            "shader_debugs": len(_runtime.shader_debugs),
        }
        if _patch_engine is not None:
            for sid in list(_runtime.replays.keys()):
                try:
                    await _patch_engine.revert_all(sid, _session_manager)
                except Exception:
                    pass
        _runtime.shader_replacements.clear()
        for sid in list(_runtime.replays.keys()):
            try:
                await _session_manager.close_session(sid)
            except Exception:
                pass
        _runtime.replays.clear()
        _runtime.captures.clear()
        _runtime.shader_debugs.clear()
        for remote_id in list(_runtime.remotes.keys()):
            handle = _runtime.remotes.pop(remote_id, None)
            if handle is not None:
                try:
                    await _offload(_disconnect_remote_handle_sync, handle)
                except Exception:
                    pass
        _runtime.session_owned_remotes.clear()
        _runtime.consumed_remotes.clear()
        _runtime.context_snapshots.clear()
        _runtime.context_states.clear()
        _runtime.hydrated_contexts.clear()
        _reset_context_snapshot()
        _runtime.initialized = False
        return _ok(released=released)

    if action == "get_version":
        version = await _core_get_version_value()
        return _ok(version=version, commit_hash=None, build_date=None)

    if action == "get_capabilities":
        detail = str(args.get("detail_level", "summary"))
        return _ok(capabilities=await _core_capabilities(detail=detail))

    if action == "set_config":
        global _artifact_store
        _require(args, "config")
        cfg = _as_dict(args.get("config"))
        applied = _apply_runtime_config(cfg)
        if _artifact_store is not None:
            _artifact_store = ArtifactStore(root=Path(str((_config or RdxConfig()).artifact.store_path)).resolve())
        return _ok(applied=applied)

    if action == "get_config":
        return _ok(config=_serialize_runtime_config())

    if action == "set_log_level":
        level = str(args.get("level", "info")).upper()
        logging.getLogger().setLevel(level)
        _apply_runtime_config({"log_level": level})
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
        log_records = read_runtime_logs(_runtime_context_id(), since_ms=_as_int(since_ms) if since_ms is not None else None, max_lines=max_lines * 4)
        if not log_records:
            log_records = list(_runtime.logs)
        for item in log_records:
            if since_ms is not None and int(item.get("ts_ms", 0)) < int(since_ms):
                continue
            lv = str(item.get("level", "info")).lower()
            lv = "warning" if lv == "warn" else lv
            idx = levels.index(lv) if lv in levels else 0
            if idx < cutoff:
                continue
            out.append(item)
        return _ok(logs=out[-max_lines:])

    if action == "get_operation_history":
        state = _context_state(_runtime_context_id())
        max_items = _as_int(args.get("max_items"), 32)
        since_ms = _as_int(args.get("since_ms"), 0)
        operation_name = str(args.get("operation") or "").strip().lower()
        status_filter = str(args.get("status") or "").strip().lower()
        items = []
        for entry in state.get("recent_operations", []):
            if not isinstance(entry, dict):
                continue
            if since_ms and int(entry.get("updated_at_ms") or 0) < since_ms:
                continue
            if operation_name and operation_name not in str(entry.get("operation") or "").lower():
                continue
            if status_filter and status_filter != str(entry.get("status") or "").lower():
                continue
            items.append(entry)
        return _ok(context_id=_runtime_context_id(), operations=items[:max_items])

    if action == "get_runtime_metrics":
        return _ok(**_runtime_metrics_payload(_runtime_context_id()))

    if action == "list_tools":
        detail_level = str(args.get("detail_level") or "summary").strip().lower() or "summary"
        tools = _filter_tool_profiles(
            namespace=str(args.get("namespace") or ""),
            group=str(args.get("group") or ""),
            capability=str(args.get("capability") or ""),
            role=str(args.get("role") or ""),
            intent=str(args.get("intent") or ""),
            mutates_state=(
                bool(args.get("mutates_state"))
                if args.get("mutates_state") is not None
                else None
            ),
            detail_level="full" if detail_level == "full" else "summary",
        )
        return _ok(tool_count=len(tools), tools=tools)

    if action == "search_tools":
        detail_level = str(args.get("detail_level") or "summary").strip().lower() or "summary"
        tools = _filter_tool_profiles(
            query=str(args.get("query") or ""),
            namespace=str(args.get("namespace") or ""),
            capability=str(args.get("capability") or ""),
            role=str(args.get("role") or ""),
            intent=str(args.get("intent") or ""),
            detail_level="full" if detail_level == "full" else "summary",
        )
        return _ok(tool_count=len(tools), tools=tools)

    if action == "get_tool_graph":
        return _ok(
            **_tool_graph_payload(
                query=str(args.get("query") or ""),
                namespace=str(args.get("namespace") or ""),
                capability=str(args.get("capability") or ""),
                role=str(args.get("role") or ""),
                intent=str(args.get("intent") or ""),
            )
        )

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
    return _renderdoc_version_value()

async def _core_capabilities(*, detail: str) -> Dict[str, Any]:
    remote_connected = any(handle.connected for handle in _runtime.remotes.values())
    remote_reason = (
        "Remote tools are disabled by config."
        if not _runtime.enable_remote
        else (
            "At least one live RenderDoc remote endpoint is connected."
            if remote_connected
            else "Requires a live RenderDoc remote endpoint."
        )
    )
    summary = {
        "replay": _capability_entry(
            True,
            reason="Bundled RenderDoc replay runtime is available.",
            optional=False,
            source="bundled_runtime",
        ),
        "remote": _capability_entry(
            bool(_runtime.enable_remote and remote_connected),
            reason=remote_reason,
            optional=True,
            source="external_dependency",
            enabled_by_config=bool(_runtime.enable_remote),
            connected_handles=sum(1 for handle in _runtime.remotes.values() if handle.connected),
        ),
        "shader_debug": _capability_entry(
            True,
            reason="Requires an opened replay session whose API reports shader debugging support.",
            optional=True,
            source="renderdoc_runtime",
        ),
        "shader_replace": _capability_entry(
            True,
            reason="Requires an opened replay session whose replay backend exposes runtime shader replacement APIs.",
            optional=True,
            source="renderdoc_runtime",
        ),
        "mesh_post_transform": _capability_entry(
            True,
            reason="Requires a replay session whose controller exposes GetPostVSData.",
            optional=True,
            source="renderdoc_runtime",
        ),
        "shader_binary_export": _capability_entry(
            True,
            reason="Requires a bound shader whose reflection exposes rawBytes.",
            optional=True,
            source="renderdoc_runtime",
        ),
        "shader_compile": _capability_entry(
            True,
            reason="Requires a replay session whose controller exposes BuildTargetShader.",
            optional=True,
            source="renderdoc_runtime",
        ),
        "counters": _capability_entry(
            True,
            reason="Requires a replay session and a capture/backend that exposes counters.",
            optional=True,
            source="renderdoc_runtime",
        ),
        "artifact_dir": _runtime.config.get("artifact_dir"),
    }
    if detail == "full":
        summary["sessions"] = len(_runtime.replays)
        summary["capture_files"] = len(_runtime.captures)
        summary["remote_connections"] = len(_runtime.remotes)
    return summary


_MACRO_GUIDE: Dict[str, Dict[str, Any]] = {
    "rd.macro.summarize_frame": {
        "canonical_tools": ["rd.event.get_actions", "rd.pipeline.get_state_summary"],
        "guidance": "Use the macro for a quick frame summary; switch to event/pipeline tools when you need exact event-level control.",
    },
    "rd.macro.find_pass_by_marker": {
        "canonical_tools": ["rd.event.search_actions", "rd.event.list_passes"],
        "guidance": "Use the macro when you only know marker text; switch to event.list_passes for structured pass ranges.",
    },
    "rd.macro.explain_pixel": {
        "canonical_tools": ["rd.debug.pixel_history", "rd.event.get_action_details", "rd.pipeline.get_state"],
        "guidance": "Use the macro for narrative explanation; use canonical tools when you need raw evidence objects.",
    },
    "rd.macro.resource_dependency_graph": {
        "canonical_tools": ["rd.event.get_action_tree", "rd.resource.get_usage", "rd.resource.get_history"],
        "guidance": "Use the macro to build a coarse causal graph first; switch to event/resource tools when you need exact edges.",
    },
    "rd.macro.find_state_change_point": {
        "canonical_tools": ["rd.pipeline.get_state", "rd.event.diff_pipeline_state"],
        "guidance": "Use the macro to search a state transition window; switch to pipeline/event diff tools when you need exact snapshots.",
    },
    "rd.macro.compare_events_report": {
        "canonical_tools": ["rd.event.diff_pipeline_state", "rd.event.get_action_details"],
        "guidance": "Use the macro for a readable diff report; switch to event diff/details tools when you need raw change objects.",
    },
    "rd.macro.find_unexpected_clear": {
        "canonical_tools": ["rd.event.search_actions"],
        "guidance": "Use the macro to hunt clear/discard anomalies quickly; switch to event search when you need precise filters.",
    },
    "rd.macro.quick_triage_missing_draw": {
        "canonical_tools": ["rd.diag.scan_common_issues", "rd.event.get_action_details", "rd.pipeline.get_state", "rd.debug.pixel_history"],
        "guidance": "Use the macro to assemble an initial missing-draw triage; switch to canonical tools when you need exact evidence objects.",
    },
    "rd.macro.build_bug_report_pack": {
        "canonical_tools": ["rd.export.repro_bundle_zip", "rd.export.markdown_report", "rd.session.get_context"],
        "guidance": "Use the macro to package a report bundle; switch to export/session tools when you need direct artifact control.",
    },
    "rd.macro.shader_hotfix_validate": {
        "canonical_tools": ["rd.shader.edit_and_replace", "rd.export.screenshot"],
        "guidance": "Use the macro for closed-loop hotfix validation; switch to shader/export tools when you need each step independently.",
    },
}


def _tool_namespace(tool_name: str) -> str:
    parts = str(tool_name or "").split(".")
    if len(parts) < 2:
        return "rd"
    return ".".join(parts[:2])


_PRIMARY_DISCOVERY_NAMESPACE_RANKS = {
    "rd.capture": 100,
    "rd.replay": 110,
    "rd.event": 120,
    "rd.pipeline": 130,
    "rd.resource": 140,
    "rd.texture": 150,
    "rd.buffer": 160,
    "rd.mesh": 170,
    "rd.shader": 180,
    "rd.debug": 190,
    "rd.export": 200,
    "rd.remote": 210,
    "rd.diag": 220,
    "rd.perf": 230,
    "rd.util": 240,
}
_MACRO_DISCOVERY_RANK = 300
_META_DISCOVERY_RANK = 400
_NAVIGATION_DISCOVERY_RANK = 500
_NAVIGATION_QUERY_TERMS = {
    "browse",
    "path",
    "tree",
    "ls",
    "navigate",
    "vfs",
    "浏览",
    "路径",
    "树",
    "结构",
    "导航",
}
_TABULAR_QUERY_TERMS = {
    "table",
    "tabular",
    "tsv",
    "spreadsheet",
    "表格",
    "制表",
}
_QUERY_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+")


def _tool_role(tool_name: str) -> str:
    if tool_name.startswith("rd.macro."):
        return "macro"
    if tool_name.startswith("rd.vfs."):
        return "navigation"
    return "canonical"


def _tool_mutates_state(tool_name: str) -> bool:
    if tool_name.startswith("rd.vfs."):
        return False
    write_prefixes = (
        "rd.core.init",
        "rd.core.shutdown",
        "rd.core.set_",
        "rd.capture.open_",
        "rd.capture.close_",
        "rd.replay.set_",
        "rd.event.set_",
        "rd.remote.connect",
        "rd.remote.disconnect",
        "rd.session.update_context",
        "rd.session.select_session",
        "rd.session.resume",
    )
    return any(tool_name.startswith(prefix) for prefix in write_prefixes)


def _tool_capabilities(tool: Dict[str, Any]) -> List[str]:
    name = str(tool.get("name") or "")
    capabilities: set[str] = set()
    for prereq in tool.get("prerequisites", []):
        if not isinstance(prereq, dict):
            continue
        requires = str(prereq.get("requires") or "").strip()
        if requires.startswith("capability."):
            capabilities.add(requires.split(".", 1)[1])
    if name.startswith("rd.remote."):
        capabilities.add("remote")
    if name.startswith("rd.debug.") or name.startswith("rd.shader."):
        capabilities.add("shader_debug")
    if name.startswith("rd.perf."):
        capabilities.add("counters")
    return sorted(capabilities)


def _tool_intents(tool: Dict[str, Any]) -> List[str]:
    name = str(tool.get("name") or "")
    group = str(tool.get("group") or "").lower()
    intents: set[str] = set()
    if name.startswith("rd.session.") or name.startswith("rd.capture.") or name.startswith("rd.replay."):
        intents.add("session")
    if name.startswith("rd.vfs.") or name.startswith("rd.core.list_tools") or name.startswith("rd.core.search_tools"):
        intents.add("discovery")
    if name.startswith("rd.remote."):
        intents.add("remote")
    if (
        name.startswith("rd.macro.")
        or name in {"rd.diag.scan_common_issues", "rd.event.get_action_details", "rd.debug.pixel_history", "rd.texture.get_histogram", "rd.texture.compute_stats"}
    ):
        intents.add("analysis")
    if name.startswith("rd.export.") or name.startswith("rd.util.pack_zip") or name in {"rd.texture.save_mip_chain"}:
        intents.add("export")
    if name.startswith("rd.debug.") or name.startswith("rd.shader."):
        intents.add("debug")
    if "analysis" in group and not name.startswith("rd.analysis."):
        intents.add("analysis")
    if not intents:
        intents.add("inspection")
    return sorted(intents)


def _tool_discovery_rank(tool: Dict[str, Any] | str) -> int:
    if isinstance(tool, dict):
        name = str(tool.get("name") or "")
    else:
        name = str(tool or "")
    if name.startswith("rd.vfs."):
        return _NAVIGATION_DISCOVERY_RANK
    if name.startswith("rd.macro."):
        return _MACRO_DISCOVERY_RANK
    if name.startswith("rd.session.") or name.startswith("rd.core."):
        return _META_DISCOVERY_RANK
    return _PRIMARY_DISCOVERY_NAMESPACE_RANKS.get(_tool_namespace(name), 250)


def _tool_recommended_for(tool_name: str) -> List[str]:
    if tool_name.startswith("rd.vfs."):
        return ["browse_only"]
    return []


def _tool_not_primary_for(tool_name: str) -> List[str]:
    if tool_name.startswith("rd.vfs."):
        return ["precise_debug", "export", "state_mutation", "automation"]
    return []


def _query_contains_term(query_text: str, terms: set[str]) -> bool:
    return any(term in query_text for term in terms)


def _query_tokens(query_text: str) -> List[str]:
    if not query_text:
        return []
    tokens = _QUERY_TOKEN_RE.findall(query_text)
    if tokens:
        return tokens
    return [query_text]


def _tool_supports_tabular_projection(entry: Dict[str, Any]) -> bool:
    supports_projection = entry.get("supports_projection")
    if not isinstance(supports_projection, dict):
        return False
    return bool(supports_projection.get("tabular"))


def _tool_search_haystack(entry: Dict[str, Any]) -> str:
    parts = [
        str(entry.get("name") or ""),
        str(entry.get("namespace") or ""),
        str(entry.get("group") or ""),
        str(entry.get("description") or ""),
        " ".join(str(item) for item in entry.get("capabilities", [])),
        " ".join(str(item) for item in entry.get("intents", [])),
    ]
    if _tool_supports_tabular_projection(entry):
        parts.append("tabular tsv projection table spreadsheet 表格 制表")
    if str(entry.get("role") or "") == "navigation":
        parts.append("browse path tree navigate vfs 浏览 路径 树 结构 导航")
    return " ".join(parts).lower()


def _tool_matches_query(entry: Dict[str, Any], query_text: str) -> bool:
    if not query_text:
        return True
    haystack = _tool_search_haystack(entry)
    if query_text in haystack:
        return True
    tokens = _query_tokens(query_text)
    if tokens and all(token in haystack for token in tokens):
        return True
    if _query_contains_term(query_text, _NAVIGATION_QUERY_TERMS) and str(entry.get("role") or "") == "navigation":
        return True
    if _query_contains_term(query_text, _TABULAR_QUERY_TERMS) and _tool_supports_tabular_projection(entry):
        return True
    return False


def _tool_query_rank_adjustment(entry: Dict[str, Any], query_text: str) -> int:
    if not query_text:
        return 0
    adjustment = 0
    if _query_contains_term(query_text, _NAVIGATION_QUERY_TERMS) and str(entry.get("role") or "") == "navigation":
        adjustment -= 450
    if _query_contains_term(query_text, _TABULAR_QUERY_TERMS) and _tool_supports_tabular_projection(entry):
        adjustment -= 275
    return adjustment


def _tool_sort_key(entry: Dict[str, Any], *, query_text: str = "") -> Tuple[int, str]:
    rank = int(entry.get("discovery_rank") or _tool_discovery_rank(entry))
    rank += _tool_query_rank_adjustment(entry, query_text)
    return (rank, str(entry.get("name") or ""))


def _tool_profile(tool: Dict[str, Any], *, detail_level: str = "summary") -> Dict[str, Any]:
    name = str(tool.get("name") or "")
    payload = {
        "name": name,
        "namespace": _tool_namespace(name),
        "group": str(tool.get("group") or ""),
        "description": str(tool.get("description") or ""),
        "role": _tool_role(name),
        "discovery_rank": _tool_discovery_rank(tool),
        "mutates_state": _tool_mutates_state(name),
        "capabilities": _tool_capabilities(tool),
        "intents": _tool_intents(tool),
        "prerequisites": list(tool.get("prerequisites") or []),
        "param_names": list(tool.get("param_names") or []),
    }
    recommended_for = _tool_recommended_for(name)
    if recommended_for:
        payload["recommended_for"] = recommended_for
    not_primary_for = _tool_not_primary_for(name)
    if not_primary_for:
        payload["not_primary_for"] = not_primary_for
    if tool.get("supports_projection") is not None:
        payload["supports_projection"] = dict(tool.get("supports_projection") or {})
    if name in _MACRO_GUIDE:
        payload["canonical_tools"] = list(_MACRO_GUIDE[name]["canonical_tools"])
        payload["guidance"] = str(_MACRO_GUIDE[name]["guidance"])
    if detail_level == "full":
        payload["parameter_raw"] = str(tool.get("parameter_raw") or "")
        payload["returns_raw"] = str(tool.get("returns_raw") or "")
    return payload


def _filter_tool_profiles(
    *,
    query: str = "",
    namespace: str = "",
    group: str = "",
    capability: str = "",
    role: str = "",
    intent: str = "",
    mutates_state: Optional[bool] = None,
    detail_level: str = "summary",
) -> List[Dict[str, Any]]:
    query_text = str(query or "").strip().lower()
    namespace_text = str(namespace or "").strip().lower()
    group_text = str(group or "").strip().lower()
    capability_text = str(capability or "").strip().lower()
    role_text = str(role or "").strip().lower()
    intent_text = str(intent or "").strip().lower()
    out: List[Dict[str, Any]] = []
    for tool in _load_tool_catalog():
        entry = _tool_profile(tool, detail_level=detail_level)
        if namespace_text and str(entry.get("namespace") or "").lower() != namespace_text:
            continue
        if group_text and group_text not in str(entry.get("group") or "").lower():
            continue
        if capability_text and capability_text not in [str(item).lower() for item in entry.get("capabilities", [])]:
            continue
        if role_text and str(entry.get("role") or "").lower() != role_text:
            continue
        if intent_text and intent_text not in [str(item).lower() for item in entry.get("intents", [])]:
            continue
        if mutates_state is not None and bool(entry.get("mutates_state")) != bool(mutates_state):
            continue
        if query_text and not _tool_matches_query(entry, query_text):
            continue
        out.append(entry)
    out.sort(key=lambda item: _tool_sort_key(item, query_text=query_text))
    return out


def _tool_graph_payload(*, query: str = "", namespace: str = "", intent: str = "", capability: str = "", role: str = "") -> Dict[str, Any]:
    tools = _filter_tool_profiles(
        query=query,
        namespace=namespace,
        intent=intent,
        capability=capability,
        role=role,
        detail_level="summary",
    )
    selected = {str(item.get("name") or "") for item in tools}
    edges: List[Dict[str, Any]] = []
    for tool in tools:
        name = str(tool.get("name") or "")
        for prereq in tool.get("prerequisites", []):
            if not isinstance(prereq, dict):
                continue
            for via_tool in prereq.get("via_tools", []):
                via_name = str(via_tool or "").strip()
                if via_name and via_name in selected:
                    edges.append(
                        {
                            "from": via_name,
                            "to": name,
                            "type": "prerequisite",
                            "requires": str(prereq.get("requires") or ""),
                        }
                    )
        for canonical in tool.get("canonical_tools", []):
            canonical_name = str(canonical or "").strip()
            if canonical_name and canonical_name in selected:
                edges.append({"from": name, "to": canonical_name, "type": "macro_expands_to"})
    return {"tools": tools, "edges": edges}


def _process_memory_bytes() -> int:
    if os.name != "nt":
        return 0
    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    if not ok:
        return 0
    return int(counters.WorkingSetSize)


def _runtime_metrics_payload(context_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id or _runtime_context_id())
    state = _sync_context_metrics(ctx)
    metrics = dict(state.get("metrics") or {})
    metrics["operation_duration_summary"] = summarize_operation_durations(metrics.get("recent_operation_duration_ms") or [])
    metrics["process_memory_bytes"] = _process_memory_bytes()
    metrics["live_runtime_session_count"] = len(_runtime.replays)
    metrics["live_runtime_capture_count"] = len(_runtime.captures)
    metrics["live_remote_count"] = len(_runtime.remotes)
    metrics["known_context_count"] = len({normalize_context_id(item) for item in list_context_ids()} | {ctx})
    metrics["current_session_id"] = str(state.get("current_session_id") or "")
    metrics["current_capture_file_id"] = str(state.get("current_capture_file_id") or "")
    return {
        "context_id": ctx,
        "limits": dict(state.get("limits") or {}),
        "metrics": metrics,
        "recovery": dict(state.get("recovery") or {}),
        "recent_operations": list(state.get("recent_operations") or []),
    }


def _runtime_baton_artifact_path(baton_id: str) -> Path:
    return artifacts_dir() / "runtime_batons" / f"{str(baton_id or '').strip()}.json"


def _session_locator_projection(snapshot: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    runtime = dict(snapshot.get("runtime") or {})
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    captures = state.get("captures") if isinstance(state.get("captures"), dict) else {}
    current_session_id = str(state.get("current_session_id") or runtime.get("session_id") or "").strip()
    session = dict(sessions.get(current_session_id) or {}) if isinstance(sessions, dict) else {}
    current_capture_file_id = str(
        state.get("current_capture_file_id")
        or session.get("capture_file_id")
        or runtime.get("capture_file_id")
        or ""
    ).strip()
    capture = dict(captures.get(current_capture_file_id) or {}) if isinstance(captures, dict) else {}
    return {
        "rdc_path": str(session.get("rdc_path") or capture.get("file_path") or capture.get("rdc_path") or "").strip(),
        "session_id": current_session_id,
        "frame_index": _as_int(runtime.get("frame_index"), _as_int(session.get("frame_index"), 0)),
        "active_event_id": _as_int(runtime.get("active_event_id"), _as_int(session.get("active_event_id"), 0)),
    }


def _context_summary(context_id: str) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id)
    snapshot = _context_snapshot(ctx)
    state = _context_state(ctx)
    return {
        "context_id": ctx,
        "entry_mode": str(snapshot.get("entry_mode") or "cli"),
        "backend": str(snapshot.get("backend") or "local"),
        "runtime_parallelism_ceiling": str(snapshot.get("runtime_parallelism_ceiling") or _runtime_parallelism_ceiling_for_backend(str(snapshot.get("backend") or "local"))),
        "runtime_owner": dict(snapshot.get("runtime_owner") or {}),
        "owner_lease": dict(snapshot.get("owner_lease") or {}),
        "active_baton": dict(snapshot.get("active_baton") or {}),
        "rehydrate_status": dict(snapshot.get("rehydrate_status") or {}),
        "session_locator": _session_locator_projection(snapshot, state),
        "current_session_id": str(state.get("current_session_id") or ""),
        "current_capture_file_id": str(state.get("current_capture_file_id") or ""),
        "session_count": len(state.get("sessions") or {}),
        "capture_count": len(state.get("captures") or {}),
        "updated_at_ms": int(snapshot.get("updated_at_ms") or 0),
    }


def _claim_runtime_owner_state(
    context_id: str,
    *,
    agent_id: str,
    force: bool = False,
    entry_mode: Optional[str] = None,
    backend: Optional[str] = None,
) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id)
    state = _context_state(ctx)
    existing = dict(state.get("runtime_owner") or {})
    existing_agent = str(existing.get("agent_id") or "").strip()
    existing_lease = str(existing.get("lease_id") or "").strip()
    existing_status = str(existing.get("status") or "unclaimed").strip() or "unclaimed"
    if existing_agent and existing_status == "claimed" and existing_agent != agent_id and not force:
        raise CoreError(
            code="runtime_owner_conflict",
            message=f"context {ctx} is already claimed by runtime owner {existing_agent}",
            category="runtime",
            details={
                "context_id": ctx,
                "runtime_owner": existing_agent,
                "owner_lease_id": existing_lease,
                "requested_runtime_owner": agent_id,
            },
        )
    lease_id = _new_id("lease")
    owner_payload = {
        "agent_id": agent_id,
        "lease_id": lease_id,
        "status": "claimed",
        "claimed_at_ms": _now_ms(),
        "released_at_ms": 0,
    }
    state["runtime_owner"] = owner_payload
    state["owner_lease"] = dict(owner_payload)
    if entry_mode is not None:
        state["entry_mode"] = _normalize_entry_mode(entry_mode, default=str(state.get("entry_mode") or "cli"))
    if backend is not None:
        state["backend"] = _normalize_backend(backend, default=str(state.get("backend") or "local"))
    _store_context_state(state, ctx)
    return _sync_context_snapshot_from_state(ctx)


def _release_runtime_owner_state(
    context_id: str,
    *,
    agent_id: str = "",
    owner_lease_id: str = "",
    force: bool = False,
) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id)
    state = _context_state(ctx)
    current = dict(state.get("runtime_owner") or {})
    current_agent = str(current.get("agent_id") or "").strip()
    current_lease_id = str(current.get("lease_id") or "").strip()
    if current_agent and not force:
        if agent_id and agent_id != current_agent:
            raise CoreError(
                code="runtime_owner_conflict",
                message=f"context {ctx} is owned by {current_agent}",
                category="runtime",
                details={
                    "context_id": ctx,
                    "runtime_owner": current_agent,
                    "owner_lease_id": current_lease_id,
                    "requested_runtime_owner": agent_id,
                },
            )
        if owner_lease_id and owner_lease_id != current_lease_id:
            raise CoreError(
                code="runtime_owner_conflict",
                message=f"context {ctx} lease mismatch",
                category="runtime",
                details={
                    "context_id": ctx,
                    "runtime_owner": current_agent,
                    "owner_lease_id": current_lease_id,
                    "requested_owner_lease_id": owner_lease_id,
                },
            )
    released = {
        "agent_id": "",
        "lease_id": "",
        "status": "released" if current_agent else "unclaimed",
        "claimed_at_ms": int(current.get("claimed_at_ms") or 0),
        "released_at_ms": _now_ms() if current_agent else int(current.get("released_at_ms") or 0),
    }
    state["runtime_owner"] = {
        "agent_id": "",
        "lease_id": "",
        "status": "unclaimed",
        "claimed_at_ms": 0,
        "released_at_ms": released["released_at_ms"],
    }
    state["owner_lease"] = released
    _store_context_state(state, ctx)
    return _sync_context_snapshot_from_state(ctx)


def _build_runtime_baton(context_id: str, *, task_goal: str, baton_id: str = "") -> Dict[str, Any]:
    ctx = normalize_context_id(context_id)
    snapshot = _context_snapshot(ctx)
    state = _context_state(ctx)
    current_session_id = str(state.get("current_session_id") or "")
    current_capture_file_id = str(state.get("current_capture_file_id") or "")
    session = dict((state.get("sessions") or {}).get(current_session_id) or {})
    remote_payload = dict(snapshot.get("remote") or {})
    remote_connect: Dict[str, Any] = {}
    if str(snapshot.get("backend") or "local") == "remote":
        remote_connect = {
            "transport": str(((session.get("remote") or {}).get("transport") or "renderdoc")),
            "host": str(((session.get("remote") or {}).get("host") or remote_payload.get("endpoint") or "")).split(":", 1)[0],
            "port": int(((session.get("remote") or {}).get("port") or 0)),
            "options_ref": "",
        }
    baton = {
        "baton_id": baton_id or _new_id("baton"),
        "context_id": ctx,
        "entry_mode": str(snapshot.get("entry_mode") or "cli"),
        "backend": str(snapshot.get("backend") or "local"),
        "runtime_owner": dict(snapshot.get("runtime_owner") or {}),
        "owner_lease": dict(snapshot.get("owner_lease") or {}),
        "capture_ref": {
            "rdc_path": str(session.get("rdc_path") or ""),
            "capture_file_id": current_capture_file_id,
            "session_id": current_session_id,
        },
        "session_locator": _session_locator_projection(snapshot, state),
        "rehydrate": {
            "required": True,
            "remote_connect": remote_connect,
            "frame_index": int((snapshot.get("runtime") or {}).get("frame_index") or 0),
            "active_event_id": int((snapshot.get("runtime") or {}).get("active_event_id") or 0),
            "focus": dict(snapshot.get("focus") or {}),
        },
        "active_baton": dict(snapshot.get("active_baton") or {}),
        "task_goal": str(task_goal or "").strip(),
        "exported_at_ms": _now_ms(),
    }
    return baton


def _export_runtime_baton_state(context_id: str, *, task_goal: str) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id)
    baton = _build_runtime_baton(ctx, task_goal=task_goal)
    if not str(((baton.get("capture_ref") or {}).get("rdc_path") or "")).strip():
        raise CoreError(
            code="runtime_baton_invalid",
            message="runtime baton export requires an active session or capture-backed rdc_path",
            category="validation",
            details={"context_id": ctx},
        )
    artifact_path = _runtime_baton_artifact_path(str(baton["baton_id"]))
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(artifact_path, baton)
    state = _context_state(ctx)
    state["active_baton"] = {
        "baton_id": str(baton["baton_id"]),
        "artifact_path": str(artifact_path),
        "task_goal": str(task_goal),
        "status": "exported",
        "exported_at_ms": int(baton["exported_at_ms"]),
    }
    state["rehydrate_status"] = {
        "status": "idle",
        "baton_id": str(baton["baton_id"]),
        "last_attempt_ms": 0,
        "last_success_ms": 0,
        "last_error": "",
    }
    _store_context_state(state, ctx)
    snapshot = _sync_context_snapshot_from_state(ctx)
    baton["artifact_path"] = str(artifact_path)
    baton["snapshot"] = snapshot
    return baton


def _load_runtime_baton(args: Dict[str, Any], *, context_id: str) -> Dict[str, Any]:
    payload = args.get("baton")
    if isinstance(payload, dict):
        baton = dict(payload)
    else:
        baton_id = str(args.get("baton_id") or "").strip()
        baton_path_text = str(args.get("baton_path") or "").strip()
        baton_path = Path(baton_path_text) if baton_path_text else _runtime_baton_artifact_path(baton_id)
        if not baton_path.is_file():
            raise CoreError(
                code="runtime_baton_invalid",
                message="runtime baton artifact not found",
                category="validation",
                details={"context_id": context_id, "baton_id": baton_id, "baton_path": str(baton_path)},
            )
        baton = json.loads(baton_path.read_text(encoding="utf-8"))
    if not isinstance(baton, dict):
        raise CoreError(
            code="runtime_baton_invalid",
            message="runtime baton payload must be an object",
            category="validation",
            details={"context_id": context_id},
        )
    if not str(((baton.get("capture_ref") or {}).get("rdc_path") or "")).strip():
        raise CoreError(
            code="runtime_baton_invalid",
            message="runtime baton missing capture_ref.rdc_path",
            category="validation",
            details={"context_id": context_id, "baton_id": str(baton.get("baton_id") or "")},
        )
    return baton


async def _rehydrate_runtime_baton_state(context_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx = normalize_context_id(context_id)
    baton = _load_runtime_baton(args, context_id=ctx)
    state = _context_state(ctx)
    state["rehydrate_status"] = {
        "status": "running",
        "baton_id": str(baton.get("baton_id") or ""),
        "last_attempt_ms": _now_ms(),
        "last_success_ms": int((state.get("rehydrate_status") or {}).get("last_success_ms") or 0),
        "last_error": "",
    }
    state["entry_mode"] = _normalize_entry_mode(baton.get("entry_mode"), default=str(state.get("entry_mode") or "cli"))
    state["backend"] = _normalize_backend(baton.get("backend"), default=str(state.get("backend") or "local"))
    _store_context_state(state, ctx)
    try:
        requested_session_id = str(((baton.get("capture_ref") or {}).get("session_id") or "")).strip()
        if requested_session_id:
            await _recover_single_session_from_state(ctx, requested_session_id)
            _select_context_session_state(requested_session_id, context_id=ctx)
        else:
            await _recover_context_sessions(ctx)
        snapshot = _context_snapshot(ctx)
        focus = dict(((baton.get("rehydrate") or {}).get("focus") or {}))
        if focus.get("pixel") is not None:
            snapshot["focus"]["pixel"] = normalize_pixel(focus.get("pixel"))
        if focus.get("resource_id"):
            snapshot["focus"]["resource_id"] = str(focus.get("resource_id") or "")
        if focus.get("shader_id"):
            snapshot["focus"]["shader_id"] = str(focus.get("shader_id") or "")
        snapshot = _store_context_snapshot(snapshot, ctx)
        state = _context_state(ctx)
        state["active_baton"] = {
            "baton_id": str(baton.get("baton_id") or ""),
            "artifact_path": str(baton.get("artifact_path") or args.get("baton_path") or _runtime_baton_artifact_path(str(baton.get("baton_id") or ""))),
            "task_goal": str(baton.get("task_goal") or ""),
            "status": "rehydrated",
            "exported_at_ms": int(baton.get("exported_at_ms") or 0),
        }
        state["rehydrate_status"] = {
            "status": "succeeded",
            "baton_id": str(baton.get("baton_id") or ""),
            "last_attempt_ms": int((state.get("rehydrate_status") or {}).get("last_attempt_ms") or _now_ms()),
            "last_success_ms": _now_ms(),
            "last_error": "",
        }
        _store_context_state(state, ctx)
        return {
            "baton": baton,
            "snapshot": _sync_context_snapshot_from_state(ctx),
        }
    except Exception as exc:
        state = _context_state(ctx)
        state["rehydrate_status"] = {
            "status": "failed",
            "baton_id": str(baton.get("baton_id") or ""),
            "last_attempt_ms": int((state.get("rehydrate_status") or {}).get("last_attempt_ms") or _now_ms()),
            "last_success_ms": int((state.get("rehydrate_status") or {}).get("last_success_ms") or 0),
            "last_error": str(exc),
        }
        _store_context_state(state, ctx)
        raise


async def _dispatch_session(action: str, args: Dict[str, Any]) -> str:
    context_id = normalize_context_id(args.get("context_id") or _runtime_context_id())

    if action == "get_context":
        snapshot = _context_snapshot(context_id)
        state = _sync_context_metrics(context_id)
        active_operation: Dict[str, Any] = {}
        try:
            from rdx.daemon.client import load_daemon_state

            daemon_state = load_daemon_state(context=context_id)
            active_operation = dict(daemon_state.get("active_operation") or {})
        except Exception:
            active_operation = {}
        return _ok(
            **snapshot,
            session_locator=_session_locator_projection(snapshot, state),
            current_session_id=str(state.get("current_session_id") or ""),
            sessions=list(state.get("sessions", {}).values()),
            recovery=dict(state.get("recovery") or {}),
            limits=dict(state.get("limits") or {}),
            active_operation=active_operation,
            recent_operations=list(state.get("recent_operations") or []),
        )

    if action == "create_context":
        requested_context = normalize_context_id(args.get("new_context_id") or args.get("target_context_id") or args.get("context_id") or context_id)
        _ensure_context_capacity(requested_context)
        state = _context_state(requested_context)
        _store_context_state(state, requested_context)
        snapshot = _sync_context_snapshot_from_state(requested_context)
        return _ok(
            **snapshot,
            current_session_id=str(state.get("current_session_id") or ""),
            sessions=list(state.get("sessions", {}).values()),
            recovery=dict(state.get("recovery") or {}),
            limits=dict(state.get("limits") or {}),
        )

    if action == "list_contexts":
        context_ids = sorted({normalize_context_id(item) for item in list_context_ids()} | {context_id})
        return _ok(context_id=context_id, contexts=[_context_summary(item) for item in context_ids])

    if action == "select_context":
        _require(args, "target_context_id")
        requested_context = normalize_context_id(args.get("target_context_id"))
        if not _context_state_exists(requested_context):
            return _err(
                f"Unknown context_id: {requested_context}",
                code="context_not_found",
                category="not_found",
                details={"context_id": requested_context},
            )
        state = _context_state(requested_context)
        snapshot = _sync_context_snapshot_from_state(requested_context)
        return _ok(
            **snapshot,
            selected_context_id=requested_context,
            current_session_id=str(state.get("current_session_id") or ""),
            sessions=list(state.get("sessions", {}).values()),
            recovery=dict(state.get("recovery") or {}),
            limits=dict(state.get("limits") or {}),
            recent_operations=list(state.get("recent_operations") or []),
        )

    if action == "clear_context":
        requested_context = normalize_context_id(args.get("target_context_id") or args.get("context_id") or context_id)
        snapshot = _reset_context_snapshot(requested_context)
        state = _context_state(requested_context)
        return _ok(
            **snapshot,
            current_session_id=str(state.get("current_session_id") or ""),
            sessions=list(state.get("sessions", {}).values()),
            recovery=dict(state.get("recovery") or {}),
            limits=dict(state.get("limits") or {}),
        )

    if action == "update_context":
        _require(args, "key")
        key = str(args["key"] or "").strip()
        try:
            snapshot = update_user_context(
                _context_snapshot(context_id),
                key,
                args.get("value"),
                retention=_snapshot_retention(),
            )
        except ValueError as exc:
            return _err(str(exc), code="validation_error", category="validation")
        snapshot = _store_context_snapshot(snapshot, context_id)
        return _ok(**snapshot)

    if action == "list_sessions":
        state = _sync_context_metrics(context_id)
        snapshot = _context_snapshot(context_id)
        return _ok(
            context_id=context_id,
            current_session_id=str(state.get("current_session_id") or ""),
            sessions=list(state.get("sessions", {}).values()),
            recovery=dict(state.get("recovery") or {}),
            limits=dict(state.get("limits") or {}),
            runtime_parallelism_ceiling=str(snapshot.get("runtime_parallelism_ceiling") or _runtime_parallelism_ceiling_for_backend(str(snapshot.get("backend") or "local"))),
            runtime_owner=dict(snapshot.get("runtime_owner") or {}),
            owner_lease=dict(snapshot.get("owner_lease") or {}),
            entry_mode=str(snapshot.get("entry_mode") or "cli"),
            backend=str(snapshot.get("backend") or "local"),
            active_baton=dict(snapshot.get("active_baton") or {}),
            rehydrate_status=dict(snapshot.get("rehydrate_status") or {}),
        )

    if action == "select_session":
        _require(args, "session_id")
        session_id = str(args["session_id"] or "").strip()
        state = _context_state(context_id)
        if session_id in state.get("sessions", {}):
            if session_id not in _runtime.replays:
                try:
                    await _ensure_live_session(session_id)
                except CoreError as exc:
                    if exc.code == "session_resume_failed":
                        return _err(
                            exc.message,
                            code=exc.code,
                            category=exc.category,
                            details=dict(exc.details),
                        )
        snapshot = _select_context_session_state(session_id, context_id=context_id)
        state = _context_state(context_id)
        return _ok(
            **snapshot,
            current_session_id=str(state.get("current_session_id") or ""),
            sessions=list(state.get("sessions", {}).values()),
            recovery=dict(state.get("recovery") or {}),
            limits=dict(state.get("limits") or {}),
            recent_operations=list(state.get("recent_operations") or []),
        )

    if action == "claim_runtime_owner":
        _require(args, "runtime_owner")
        runtime_owner = str(args.get("runtime_owner") or "").strip()
        try:
            snapshot = _claim_runtime_owner_state(
                context_id,
                agent_id=runtime_owner,
                force=_as_bool(args.get("force"), False),
                entry_mode=args.get("entry_mode"),
                backend=args.get("backend"),
            )
        except CoreError as exc:
            return _err(exc.message, code=exc.code, category=exc.category, details=dict(exc.details))
        return _ok(
            **snapshot,
        )

    if action == "release_runtime_owner":
        try:
            snapshot = _release_runtime_owner_state(
                context_id,
                agent_id=str(args.get("runtime_owner") or "").strip(),
                owner_lease_id=str(args.get("owner_lease_id") or "").strip(),
                force=_as_bool(args.get("force"), False),
            )
        except CoreError as exc:
            return _err(exc.message, code=exc.code, category=exc.category, details=dict(exc.details))
        return _ok(
            **snapshot,
        )

    if action == "export_runtime_baton":
        _require(args, "task_goal")
        try:
            payload = _export_runtime_baton_state(context_id, task_goal=str(args.get("task_goal") or "").strip())
        except CoreError as exc:
            return _err(exc.message, code=exc.code, category=exc.category, details=dict(exc.details))
        return _ok(
            context_id=context_id,
            baton_id=str(payload.get("baton_id") or ""),
            artifact_path=str(payload.get("artifact_path") or ""),
            runtime_owner=dict(payload.get("runtime_owner") or {}),
            owner_lease=dict(payload.get("owner_lease") or {}),
            entry_mode=str(payload.get("entry_mode") or "cli"),
            backend=str(payload.get("backend") or "local"),
            session_locator=dict(payload.get("session_locator") or {}),
            active_baton=dict((payload.get("snapshot") or {}).get("active_baton") or {}),
            rehydrate_status=dict((payload.get("snapshot") or {}).get("rehydrate_status") or {}),
            baton=payload,
        )

    if action == "rehydrate_runtime_baton":
        try:
            payload = await _rehydrate_runtime_baton_state(context_id, args)
        except CoreError as exc:
            return _err(exc.message, code=exc.code, category=exc.category, details=dict(exc.details))
        except Exception as exc:  # noqa: BLE001
            return _err(
                str(exc),
                code="runtime_baton_invalid",
                category="runtime",
                details={"context_id": context_id},
            )
        snapshot = dict(payload.get("snapshot") or {})
        return _ok(
            **snapshot,
            baton=dict(payload.get("baton") or {}),
        )

    if action == "resume":
        requested_session_id = str(args.get("session_id") or "").strip()
        await _recover_context_sessions(context_id)
        if requested_session_id:
            state = _context_state(context_id)
            session = dict(state.get("sessions", {}).get(requested_session_id) or {})
            if not session:
                return _err(
                    f"Unknown session_id: {requested_session_id}",
                    code="session_not_found",
                    category="not_found",
                    details={"session_id": requested_session_id},
                )
            if not bool(session.get("is_live")):
                try:
                    await _ensure_live_session(requested_session_id)
                except CoreError as exc:
                    if exc.code == "session_resume_failed":
                        return _err(
                            exc.message,
                            code=exc.code,
                            category=exc.category,
                            details=dict(exc.details),
                        )
                state = _context_state(context_id)
                session = dict(state.get("sessions", {}).get(requested_session_id) or {})
                if not bool(session.get("is_live")):
                    return _err(
                        f"Session could not be resumed: {requested_session_id}",
                        code="session_resume_failed",
                        category="runtime",
                        details={"session_id": requested_session_id, "last_error": str(session.get("last_error") or "")},
                    )
            _select_context_session_state(requested_session_id, context_id=context_id)
        state = _context_state(context_id)
        snapshot = _context_snapshot(context_id)
        return _ok(
            **snapshot,
            current_session_id=str(state.get("current_session_id") or ""),
            sessions=list(state.get("sessions", {}).values()),
            recovery=dict(state.get("recovery") or {}),
            limits=dict(state.get("limits") or {}),
            recent_operations=list(state.get("recent_operations") or []),
        )

    return _err(f"Unsupported session action: {action}")


async def _dispatch_capture(action: str, args: Dict[str, Any]) -> str:
    if action == "open_file":
        _require(args, "file_path")
        file_path = str(args["file_path"])
        read_only = _as_bool(args.get("read_only"), True)
        path = Path(file_path)
        if not path.is_file():
            return _err(f"Capture file not found: {file_path}")
        state = _context_state(_runtime_context_id())
        limits = dict(state.get("limits") or {})
        file_meta = _capture_file_metadata(str(path))
        if int(file_meta.get("file_size_bytes") or 0) > int(limits.get("max_capture_size_bytes") or 0):
            metrics = dict(state.get("metrics") or {})
            metrics["rejection_count"] = int(metrics.get("rejection_count") or 0) + 1
            state["metrics"] = metrics
            _store_context_state(state, _runtime_context_id())
            return _err(
                f"Capture file exceeds limit: {file_path}",
                code="capture_file_too_large",
                category="runtime",
                details={
                    "file_path": str(path),
                    "file_size_bytes": int(file_meta.get("file_size_bytes") or 0),
                    "max_capture_size_bytes": int(limits.get("max_capture_size_bytes") or 0),
                },
            )
        if len(state.get("captures", {})) >= int(limits.get("max_capture_files") or 1):
            metrics = dict(state.get("metrics") or {})
            metrics["rejection_count"] = int(metrics.get("rejection_count") or 0) + 1
            state["metrics"] = metrics
            _store_context_state(state, _runtime_context_id())
            return _err(
                "Capture file limit exceeded",
                code="capture_limit_exceeded",
                category="runtime",
                details={
                    "max_capture_files": int(limits.get("max_capture_files") or 0),
                    "active_capture_count": len(state.get("captures", {})),
                },
            )
        _progress(
            "capture_file_validated",
            "Capture file validated",
            progress_pct=0.1,
            details={
                "file_path": str(path),
                "file_size_bytes": int(file_meta.get("file_size_bytes") or 0),
            },
        )
        driver = ""
        try:
            rd = _get_rd()
            cap = await _offload(rd.OpenCaptureFile)
            status = await _offload(cap.OpenFile, str(path), "", None)
            _check_status(status, "OpenFile")
            try:
                if hasattr(cap, "DriverName"):
                    driver = str(await _offload(cap.DriverName) or "")
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
        _set_context_capture_file(capture_file_id)
        return _ok(capture_file_id=capture_file_id, driver=driver)

    if action == "close_file":
        _require(args, "capture_file_id")
        capture_file_id = str(args["capture_file_id"])
        if capture_file_id not in _runtime.captures:
            return _err(f"Unknown capture_file_id: {capture_file_id}")
        dependent_session_ids = _capture_dependent_session_ids(capture_file_id)
        if dependent_session_ids:
            return _err(
                f"Capture file still in use: {capture_file_id}",
                code="capture_file_in_use",
                category="runtime",
                details={
                    "capture_file_id": capture_file_id,
                    "dependent_session_ids": dependent_session_ids,
                    "dependent_session_count": len(dependent_session_ids),
                },
            )
        _runtime.captures.pop(capture_file_id, None)
        _clear_context_capture_file(capture_file_id)
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
        state = _context_state(_runtime_context_id())
        limits = dict(state.get("limits") or {})
        session_count = len(state.get("sessions", {}))
        if session_count >= int(limits.get("max_sessions_per_context") or 1):
            metrics = dict(state.get("metrics") or {})
            metrics["rejection_count"] = int(metrics.get("rejection_count") or 0) + 1
            state["metrics"] = metrics
            _store_context_state(state, _runtime_context_id())
            return _err(
                "Session limit exceeded for current context",
                code="session_limit_exceeded",
                category="runtime",
                details={
                    "max_sessions_per_context": int(limits.get("max_sessions_per_context") or 0),
                    "active_session_count": session_count,
                    "context_id": _runtime_context_id(),
                },
            )
        file_meta = _capture_file_metadata(handle.file_path)
        estimated_replay_memory_bytes = _estimated_replay_memory_bytes(int(file_meta.get("file_size_bytes") or 0))
        if estimated_replay_memory_bytes > int(limits.get("max_estimated_replay_memory_bytes") or 0):
            metrics = dict(state.get("metrics") or {})
            metrics["rejection_count"] = int(metrics.get("rejection_count") or 0) + 1
            state["metrics"] = metrics
            _store_context_state(state, _runtime_context_id())
            return _err(
                "Estimated replay memory exceeds limit",
                code="replay_memory_limit_exceeded",
                category="runtime",
                details={
                    "capture_file_id": capture_file_id,
                    "file_size_bytes": int(file_meta.get("file_size_bytes") or 0),
                    "estimated_replay_memory_bytes": estimated_replay_memory_bytes,
                    "max_estimated_replay_memory_bytes": int(limits.get("max_estimated_replay_memory_bytes") or 0),
                },
            )
        _progress("capture_open_started", "Opening replay session", progress_pct=0.55, details={"capture_file_id": capture_file_id})
        options = _as_dict(args.get("options"), default={})
        remote_id = str(options.get("remote_id") or "").strip()
        backend_type = "local"
        backend_config: Dict[str, Any] = {"type": "local"}
        remote_handle_for_session: RemoteHandle | None = None
        remote_endpoint = ""
        if remote_id:
            consumed = _remote_consumed_payload(remote_id)
            if consumed is not None:
                return consumed
            remote_handle = _runtime.remotes.get(remote_id)
            if remote_handle is None:
                return _err(
                    f"Unknown remote_id: {remote_id}",
                    code="remote_not_found",
                    category="runtime",
                    details={"remote_id": remote_id},
                )
            if not remote_handle.connected or remote_handle.remote_server is None:
                return _err(
                    f"Remote handle {remote_id} is not connected",
                    code="remote_not_connected",
                    category="runtime",
                    details={"remote_id": remote_id, "endpoint": _remote_url(remote_handle.host, remote_handle.port)},
                )
            remote_handle_for_session = remote_handle
            remote_endpoint = _remote_url(remote_handle.host, remote_handle.port)
            backend_type = "remote"
            backend_config = {
                "type": "remote",
                "host": remote_handle.host,
                "port": remote_handle.port,
                "transport": remote_handle.transport,
                "remote_id": remote_id,
                "remote_server": remote_handle.remote_server,
                "close_remote_server_on_cleanup": False,
            }
        session_info = await _session_manager.create_session(
            backend_config=backend_config,
            replay_config={},
        )
        _progress("session_created", "Replay session allocated", progress_pct=0.72, details={"session_id": session_info.session_id, "backend_type": backend_type})
        try:
            cap_info = await _session_manager.open_capture(session_info.session_id, handle.file_path)
            _progress("capture_open_done", "Capture opened for replay", progress_pct=0.82, details={"session_id": session_info.session_id})
            # The session already exists in SessionManager after open_capture().
            # Avoid recovery-aware helpers until _runtime.replays/context state is
            # populated for the first time, otherwise a fresh session can be
            # misclassified as missing and surface as session_not_found.
            controller = _session_manager.get_controller(session_info.session_id)
            roots = await _offload(controller.GetRootActions)
            active_event_id = int(getattr(roots[0], "eventId", 0)) if roots else 0
            _progress("root_actions_loaded", "Frame actions loaded", progress_pct=0.91, details={"active_event_id": active_event_id})
            _runtime.replays[session_info.session_id] = ReplayHandle(
                session_id=session_info.session_id,
                capture_file_id=capture_file_id,
                frame_index=0,
                active_event_id=active_event_id,
            )
            if remote_handle_for_session is not None:
                _register_remote_session_lease(session_info.session_id, remote_handle_for_session)
            _set_context_runtime_session(
                session_info.session_id,
                capture_file_id=capture_file_id,
                backend_type=backend_type,
                frame_index=0,
                active_event_id=active_event_id,
                remote_metadata=(
                    _remote_session_metadata(
                        remote_handle_for_session,
                        remote_id=remote_id,
                        endpoint=remote_endpoint,
                    )
                    if remote_handle_for_session is not None
                    else {}
                ),
            )
            _progress("context_synced", "Replay context synchronized", progress_pct=1.0, details={"session_id": session_info.session_id, "capture_file_id": capture_file_id})
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
        except Exception:
            try:
                await _session_manager.close_session(session_info.session_id)
            except Exception:
                pass
            _runtime.replays.pop(session_info.session_id, None)
            if remote_handle_for_session is not None:
                _release_remote_session_lease(session_info.session_id)
                _set_context_remote_live(remote_id, remote_endpoint)
            raise
    if action == "close_replay":
        _require(args, "session_id")
        session_id = str(args["session_id"])
        owned_remote = _release_remote_session_lease(session_id)
        if _patch_engine is not None:
            try:
                await _patch_engine.revert_all(session_id, _session_manager)
            except Exception:
                pass
        _runtime.shader_replacements.pop(session_id, None)
        _runtime.replays.pop(session_id, None)
        await _session_manager.close_session(session_id)
        if owned_remote is not None and owned_remote.remote_id in _runtime.remotes:
            _set_context_remote_live(owned_remote.remote_id, _remote_url(owned_remote.host, owned_remote.port))
        _clear_context_runtime(session_id)
        return _ok()

    return _err(f"Unsupported capture action: {action}")


async def _dispatch_replay(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])
    await _ensure_live_session(session_id)
    replay = _get_replay_handle(session_id)
    controller = await _get_controller(session_id)

    if action == "set_frame":
        replay.frame_index = _as_int(args.get("frame_index"), 0)
        roots = await _offload(controller.GetRootActions)
        active_event_id = _pick_default_event_id(roots)
        if active_event_id > 0:
            await _offload(controller.SetFrameEvent, active_event_id, True)
        replay.active_event_id = active_event_id
        _set_context_frame(session_id, replay.frame_index, active_event_id)
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

    roots, flat, by_event = await _load_action_index(session_id, controller=controller)

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
        resolved_event = _require_action_event(session_id, event_id, by_event)
        await _offload(controller.SetFrameEvent, resolved_event, True)
        _store_active_event(session_id, resolved_event)
        return _ok(active_event_id=resolved_event)

    if action == "get_active":
        return _ok(active_event_id=_active_event(session_id))

    if action == "get_actions":
        include_markers = _as_bool(args.get("include_markers"), True)
        include_drawcalls = _as_bool(args.get("include_drawcalls"), True)
        max_nodes = max(1, _as_int(args.get("max_nodes"), 2000))
        emitted = 0

        def bounded_action(action_obj: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
            nonlocal emitted
            if emitted >= max_nodes:
                return None
            item = _action_to_dict(action_obj, include_children=False, depth=depth)
            flags = item.get("flags", {})
            if flags.get("is_marker") and not include_markers:
                return None
            if flags.get("is_draw") and not include_drawcalls:
                return None
            emitted += 1
            children = []
            for child in getattr(action_obj, "children", None) or []:
                if emitted >= max_nodes:
                    break
                child_item = bounded_action(child, depth + 1)
                if child_item is not None:
                    children.append(child_item)
            item["children"] = children
            return item

        out = []
        for root in roots:
            if emitted >= max_nodes:
                break
            item = bounded_action(root, 0)
            if item is not None:
                out.append(item)
        return _ok(actions=out, pagination={"max_nodes": max_nodes, "emitted_nodes": emitted, "truncated": emitted >= max_nodes})

    if action == "get_action_tree":
        max_depth = args.get("max_depth")
        filter_cfg = _as_dict(args.get("filter"), default={})
        name_contains = str(filter_cfg.get("name_contains", "")).strip().lower()
        offset = max(0, _as_int(args.get("offset"), 0))
        limit = max(1, _as_int(args.get("limit"), 256))
        max_nodes = max(1, _as_int(args.get("max_nodes"), 2000))
        emitted = 0

        def trim(node: Dict[str, Any], depth: int) -> Optional[Dict[str, Any]]:
            nonlocal emitted
            if emitted >= max_nodes:
                return None
            if max_depth is not None and depth > int(max_depth):
                return None
            if name_contains and name_contains not in str(node.get("name", "")).lower():
                pass
            emitted += 1
            children = [trim(c, depth + 1) for c in node.get("children", [])]
            node["children"] = [c for c in children if c is not None]
            return node

        root_payload = {"event_id": 0, "name": "root", "flags": {}, "children": []}
        selected_roots = list(roots)[offset : offset + limit]
        for r in selected_roots:
            if emitted >= max_nodes:
                break
            node = trim(_action_to_dict(r, include_children=True, depth=1), 1)
            if node is not None:
                root_payload["children"].append(node)
        return _ok(
            root=root_payload,
            pagination={
                "offset": offset,
                "limit": limit,
                "max_nodes": max_nodes,
                "returned_root_count": len(root_payload["children"]),
                "total_root_count": len(list(roots)),
                "truncated": emitted >= max_nodes,
            },
        )

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
    resolved_event_id = await _ensure_event(session_id, _as_int(args["event_id"]) if args.get("event_id") is not None else None)
    assert _pipeline_service is not None
    snapshot = await _pipeline_service.snapshot_pipeline(
        session_id=session_id,
        event_id=resolved_event_id,
        session_manager=_session_manager,
    )
    snapshot_dict = snapshot.model_dump(mode="json")
    controller = await _get_controller(session_id)
    if resolved_event_id > 0:
        await _offload(controller.SetFrameEvent, resolved_event_id, True)
    pipe = await _offload(controller.GetPipelineState)
    def _pipeline_ok(**fields: Any) -> str:
        return _ok(resolved_event_id=int(resolved_event_id), **fields)

    if action == "get_state":
        return _pipeline_ok(pipeline_state=snapshot_dict)
    if action == "get_state_summary":
        summary = {
            "api": snapshot_dict.get("api"),
            "shaders": snapshot_dict.get("shaders", []),
            "render_targets": snapshot_dict.get("render_targets", []),
            "binding_count": len(snapshot_dict.get("bindings", [])),
            "topology": snapshot_dict.get("topology", ""),
            "viewport": snapshot_dict.get("viewport", {}),
        }
        return _pipeline_ok(summary=summary)
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
        return _pipeline_ok(stage_state=state)
    if action == "get_vertex_input":
        return _pipeline_ok(ia={"topology": snapshot_dict.get("topology"), "vertex_buffers": snapshot_dict.get("bindings", [])})
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
        return _pipeline_ok(vertex_buffers=vbs)
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
        return _pipeline_ok(index_buffer=index_buffer)
    if action == "get_primitive_topology":
        return _pipeline_ok(topology={"topology": snapshot_dict.get("topology", "")})
    if action == "get_viewports_scissors":
        return _pipeline_ok(viewports=[snapshot_dict.get("viewport", {})], scissors=[snapshot_dict.get("scissor", {})])
    if action == "get_rasterizer_state":
        return _pipeline_ok(rasterizer={})
    if action == "get_multisample_state":
        return _pipeline_ok(multisample={})
    if action == "get_blend_state":
        return _pipeline_ok(blend={"states": snapshot_dict.get("blend_states", [])})
    if action == "get_depth_stencil_state":
        return _pipeline_ok(depth_stencil=snapshot_dict.get("depth_stencil", {}))
    if action == "get_output_targets":
        return _pipeline_ok(framebuffer={"render_targets": snapshot_dict.get("render_targets", []), "depth_target": snapshot_dict.get("depth_target")})
    if action == "get_render_targets":
        return _pipeline_ok(render_targets=snapshot_dict.get("render_targets", []))
    if action == "get_depth_target":
        return _pipeline_ok(depth_target=snapshot_dict.get("depth_target"))
    if action == "get_resource_bindings":
        bindings = await _pipeline_service.get_resource_bindings(session_id, resolved_event_id, _session_manager)
        return _pipeline_ok(bindings=[b.model_dump(mode="json") for b in bindings])
    if action == "get_uav_bindings":
        all_bindings = await _pipeline_service.get_resource_bindings(session_id, resolved_event_id, _session_manager)
        uavs = [b.model_dump(mode="json") for b in all_bindings if b.type.upper() == "UAV"]
        return _pipeline_ok(uavs=uavs)
    if action == "get_sampler_bindings":
        return _pipeline_ok(samplers=[])
    if action == "get_constant_buffers":
        return _pipeline_ok(constant_buffers=[])
    if action == "get_push_constants":
        return _pipeline_ok(push_constants=[])
    if action == "get_dynamic_state":
        return _pipeline_ok(dynamic_state={})
    if action == "get_root_signature":
        return _pipeline_ok(root_signature={})
    if action == "get_descriptor_heaps":
        return _pipeline_ok(descriptor_heaps=[])
    if action == "get_resource_states":
        return _pipeline_ok(resource_states=[])
    if action == "get_shader":
        rd_stage = _rd_stage(stage)
        shader_id = await _offload(pipe.GetShader, rd_stage)
        if _is_null_resource_id(shader_id):
            return _err(
                f"No shader bound at stage {stage.upper()} for event {int(resolved_event_id)}",
                code="shader_not_bound",
                category="runtime",
                details={"session_id": session_id, "resolved_event_id": int(resolved_event_id), "stage": stage.upper()},
            )
        reflection = await _offload(pipe.GetShaderReflection, rd_stage)
        shader = {
            "stage": stage.upper(),
            "shader_id": str(shader_id),
            "entry": str(getattr(reflection, "entryPoint", "")) if reflection else "",
            "encoding": str(getattr(reflection, "encoding", "")) if reflection else "",
        }
        return _pipeline_ok(shader=shader)
    return _err(f"Unsupported pipeline action: {action}")


async def _dispatch_resource(action: str, args: Dict[str, Any]) -> str:
    _require(args, "session_id")
    session_id = str(args["session_id"])
    controller = await _get_controller(session_id)
    binding_index_cache: Optional[Dict[str, List[str]]] = None
    event_lookup_cache: Optional[Dict[int, Any]] = None

    async def get_binding_index() -> Dict[str, List[str]]:
        nonlocal binding_index_cache
        if binding_index_cache is None:
            binding_index_cache = await _binding_name_index_for_event(
                session_id,
                _active_event(session_id),
            )
        return binding_index_cache

    async def get_event_lookup() -> Dict[int, Any]:
        nonlocal event_lookup_cache
        if event_lookup_cache is None:
            _, _, event_lookup_cache = await _load_action_index(session_id, controller=controller)
        return event_lookup_cache

    async def usage_event_payload(entry: Any) -> Dict[str, Any]:
        raw_event_id = int(getattr(entry, "eventId", 0))
        event_lookup = await get_event_lookup()
        resolvable = raw_event_id > 0 and raw_event_id in event_lookup
        return {
            "event_id": raw_event_id if resolvable else None,
            "raw_event_id": raw_event_id,
            "event_resolvable": resolvable,
        }

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
            event_info = await usage_event_payload(entry)
            usage.append(
                {
                    **event_info,
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
            event_info = await usage_event_payload(entry)
            history.append(
                {
                    **event_info,
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
                response = await _export_texture_file(
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
                return _ok(
                    **{
                        key: {
                            "artifact_path": payload.get("artifact_path"),
                            "saved_path": payload.get("saved_path"),
                            "content_kind": payload.get("content_kind"),
                            "container_format": payload.get("container_format"),
                            "stats": payload.get("stats"),
                        }
                    }
                )
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


def _texture_export_channels(channels_value: Any) -> Dict[str, bool]:
    if isinstance(channels_value, str):
        selected = {token for token in channels_value.lower() if token in {"r", "g", "b", "a"}}
        if not selected:
            selected = {"r", "g", "b", "a"}
        return {token: token in selected for token in ("r", "g", "b", "a")}
    if isinstance(channels_value, dict):
        return {
            "r": _as_bool(channels_value.get("r"), True),
            "g": _as_bool(channels_value.get("g"), True),
            "b": _as_bool(channels_value.get("b"), True),
            "a": _as_bool(channels_value.get("a"), True),
        }
    return {"r": True, "g": True, "b": True, "a": True}


def _texture_export_view_config(args: Dict[str, Any], *, subresource: Dict[str, int]) -> Dict[str, Any]:
    remap = _as_dict(args.get("remap"), default={})
    view_config: Dict[str, Any] = {
        "channels": _texture_export_channels(args.get("channels")),
        "flip_y": _as_bool(args.get("flip_y"), False),
        "subresource": dict(subresource),
    }
    if remap.get("black_point") is not None:
        view_config["range_min"] = _as_float(remap.get("black_point"), 0.0)
    if remap.get("white_point") is not None:
        view_config["range_max"] = _as_float(remap.get("white_point"), 1.0)
    if remap.get("hdr_multiplier") is not None:
        view_config["hdr_multiplier"] = _as_float(remap.get("hdr_multiplier"), 4.0)
    if remap.get("hdr_clamp") is not None:
        view_config["hdr"] = _as_bool(remap.get("hdr_clamp"), False)
    return view_config


def _texture_export_uses_display_controls(args: Dict[str, Any]) -> bool:
    remap = _as_dict(args.get("remap"), default={})
    return args.get("channels") is not None or args.get("flip_y") is not None or bool(remap)


async def _render_texture_export(
    *,
    session_id: str,
    event_id: int,
    texture_id: Any,
    output_path: str,
    file_format: str,
    subresource: Dict[str, int],
    view_config: Dict[str, Any],
) -> Dict[str, Any]:
    assert _render_service is not None
    artifact_ref, meta = await _render_service.render_event(
        session_id=session_id,
        event_id=event_id,
        session_manager=_session_manager,
        artifact_store=_artifact_store,
        source_config={"source": "texture", "texture_id": texture_id, "subresource": dict(subresource)},
        view_config={**view_config, "overlay": "none"},
        output_format=file_format,
    )
    artifact_path = _artifact_path(artifact_ref)
    out_path = Path(str(output_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(artifact_path, out_path)
    export_meta = dict(meta)
    export_meta["requested_remap"] = dict(_as_dict(view_config.get("requested_remap"), default={}))
    return {
        "artifact_path": artifact_path,
        "saved_path": str(out_path),
        "meta": export_meta,
    }


async def _save_texture_export(
    *,
    session_id: str,
    event_id: int,
    texture_id: Any,
    output_path: str,
    file_format: str,
    subresource: Dict[str, int],
) -> Dict[str, Any]:
    assert _render_service is not None
    artifact_ref, meta, saved_path = await _render_service.save_texture_file(
        session_id=session_id,
        event_id=event_id,
        texture_id=texture_id,
        session_manager=_session_manager,
        artifact_store=_artifact_store,
        output_format=file_format,
        output_path=output_path,
        subresource=subresource,
    )
    return {
        "artifact_path": _artifact_path(artifact_ref),
        "saved_path": saved_path or str(output_path),
        "meta": meta,
    }


async def _export_texture_file(args: Dict[str, Any]) -> str:
    _require(args, "session_id", "texture_id", "output_path")
    session_id = str(args["session_id"])
    event_id = _as_int(args.get("event_id"), _active_event(session_id))
    if event_id <= 0:
        event_id = await _ensure_event(session_id, None)
    texture_id, texture_desc = await _get_texture_descriptor(
        session_id,
        args.get("texture_id"),
        event_id=event_id,
    )
    binding_index = await _binding_name_index_for_event(session_id, event_id)
    name_info = _compose_texture_name_info(
        texture_id,
        resource_name=str(getattr(texture_desc, "name", "")) if texture_desc is not None else "",
        binding_names=binding_index.get(str(texture_id), []),
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
    base_output_path = str(args["output_path"])
    multi_export = len(selected_formats) > 1
    uses_display_controls = _texture_export_uses_display_controls(args)
    view_config = _texture_export_view_config(args, subresource=normalized_subresource)
    view_config["requested_remap"] = _as_dict(args.get("remap"), default={})
    exports: List[Dict[str, Any]] = []
    for export_format in selected_formats:
        resolved_output_path = _resolve_export_output_path(
            base_output_path,
            name_stem=base_name_stem,
            file_format=export_format,
            multi=multi_export,
        )
        if uses_display_controls:
            if export_format not in {"png", "jpg", "exr", "hdr"}:
                return _err(
                    "Display-mapped texture export only supports png, jpg, exr, hdr when channels/remap/flip_y are requested",
                )
            exports.append(
                await _render_texture_export(
                    session_id=session_id,
                    event_id=event_id,
                    texture_id=texture_id,
                    output_path=resolved_output_path,
                    file_format=export_format,
                    subresource=normalized_subresource,
                    view_config=view_config,
                )
            )
            continue
        exports.append(
            await _save_texture_export(
                session_id=session_id,
                event_id=event_id,
                texture_id=texture_id,
                output_path=resolved_output_path,
                file_format=export_format,
                subresource=normalized_subresource,
            )
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


async def _export_buffer_file(args: Dict[str, Any]) -> str:
    _require(args, "session_id", "buffer_id", "output_path")
    session_id = str(args["session_id"])
    controller = await _get_controller(session_id)
    rid = await _resolve_resource_id(session_id, args["buffer_id"])
    offset = _as_int(args.get("offset"), 0)
    size = args.get("size")
    if size is None:
        size = 0
    data = await _offload(controller.GetBufferData, rid, offset, _as_int(size, 0))
    out = Path(str(args["output_path"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return _ok(saved_path=str(out), byte_size=len(data))


async def _export_mesh_file(args: Dict[str, Any]) -> str:
    _require(args, "session_id", "output_path")
    session_id = str(args["session_id"])
    event_id = _as_int(args.get("event_id"), _active_event(session_id))
    if event_id <= 0:
        event_id = await _ensure_event(session_id, None)
    config_resp = await _dispatch_mesh("get_drawcall_mesh_config", {"session_id": session_id, "event_id": event_id})
    payload = json.loads(config_resp)
    if not payload.get("success"):
        return config_resp
    export_format = str(args.get("format", "obj")).lower()
    include_attributes = _as_bool(args.get("include_attributes"), True)
    space = str(args.get("space", "postvs")).lower()
    out = Path(str(args["output_path"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "format": export_format,
                "include_attributes": include_attributes,
                "space": space,
                "mesh_config": payload.get("mesh_config", {}),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return _ok(saved_path=str(out), export_format=export_format, include_attributes=include_attributes, space=space)


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


def _resource_format_payload(fmt: Any) -> Dict[str, Any]:
    if fmt is None:
        return {}
    payload: Dict[str, Any] = {}
    for attr in ("type", "compCount", "compByteWidth", "compType", "special", "bgraOrder", "srgbCorrected"):
        if not hasattr(fmt, attr):
            continue
        value = getattr(fmt, attr)
        if isinstance(value, bool):
            payload[attr] = bool(value)
        else:
            try:
                payload[attr] = int(value)
            except Exception:
                payload[attr] = str(value)
    payload["repr"] = str(fmt)
    return payload


def _mesh_format_payload(mesh: Any) -> Dict[str, Any]:
    return {
        "vertex_resource_id": str(getattr(mesh, "vertexResourceId", "") or ""),
        "vertex_byte_offset": int(getattr(mesh, "vertexByteOffset", 0) or 0),
        "vertex_byte_size": int(getattr(mesh, "vertexByteSize", 0) or 0),
        "vertex_byte_stride": int(getattr(mesh, "vertexByteStride", 0) or 0),
        "index_resource_id": str(getattr(mesh, "indexResourceId", "") or ""),
        "index_byte_offset": int(getattr(mesh, "indexByteOffset", 0) or 0),
        "index_byte_size": int(getattr(mesh, "indexByteSize", 0) or 0),
        "index_byte_stride": int(getattr(mesh, "indexByteStride", 0) or 0),
        "num_indices": int(getattr(mesh, "numIndices", 0) or 0),
        "topology": str(getattr(mesh, "topology", "") or ""),
        "status": str(getattr(mesh, "status", "") or ""),
        "format": _resource_format_payload(getattr(mesh, "format", None)),
    }


def _mesh_vertex_rows(raw: bytes, *, stride: int, max_vertices: int) -> List[Dict[str, Any]]:
    if stride <= 0:
        return []
    rows: List[Dict[str, Any]] = []
    total_vertices = len(raw) // stride
    count = total_vertices if max_vertices <= 0 else min(total_vertices, max_vertices)
    for vertex_index in range(count):
        start = vertex_index * stride
        end = start + stride
        chunk = raw[start:end]
        rows.append(
            {
                "vertex_index": vertex_index,
                "bytes_hex": chunk.hex(),
            }
        )
    return rows


async def _dispatch_mesh(action: str, args: Dict[str, Any]) -> str:
    if action == "get_drawcall_mesh_config":
        _require(args, "session_id", "event_id")
        session_id = str(args["session_id"])
        event_id = _as_int(args["event_id"])
        snap = await _pipeline_service.snapshot_pipeline(session_id, event_id, _session_manager)
        return _ok(mesh_config={"event_id": event_id, "topology": snap.topology, "bindings": [b.model_dump(mode="json") for b in snap.bindings]})
    if action in {"get_post_vs_data", "get_post_gs_data"}:
        _require(args, "session_id")
        session_id = str(args["session_id"])
        controller = await _get_controller(session_id)
        event_id = await _ensure_event(
            session_id,
            _as_int(args.get("event_id"), 0) if args.get("event_id") is not None else None,
        )
        rd = _get_rd()
        stage = rd.MeshDataStage.VSOut if action == "get_post_vs_data" else rd.MeshDataStage.GSOut
        instance = _as_int(args.get("instance"), 0)
        view_index = _as_int(args.get("view_index", args.get("view", 0)), 0)
        max_vertices = _as_int(args.get("max_vertices"), 0)
        try:
            mesh = await _offload(controller.GetPostVSData, instance, view_index, stage)
        except Exception as exc:
            return _err(
                f"Failed to fetch post-transform mesh data: {exc}",
                code="mesh_post_transform_failed",
                category="runtime",
                details={"session_id": session_id, "resolved_event_id": int(event_id), "stage": str(stage)},
            )
        vertex_resource_id = getattr(mesh, "vertexResourceId", None)
        if _is_null_resource_id(vertex_resource_id):
            mesh_format = _mesh_format_payload(mesh)
            if action == "get_post_gs_data":
                pipe = await _get_output(session_id)
                gs_shader_id = await _offload(pipe.GetShader, _rd_stage("gs"))
                if _is_null_resource_id(gs_shader_id):
                    return _ok(
                        mesh_data={
                            "mesh_format": mesh_format,
                            "vertex_rows": [],
                            "vertex_count": 0,
                            "truncated": False,
                            "stage": "GS",
                            "stage_bound": False,
                            "availability_reason": f"No shader bound at stage GS for event {int(event_id)}",
                        },
                        resolved_event_id=int(event_id),
                    )
            return _err(
                "Post-transform mesh data is unavailable for the requested draw",
                code="mesh_post_transform_unavailable",
                category="runtime",
                details={
                    "session_id": session_id,
                    "resolved_event_id": int(event_id),
                    "stage": str(stage),
                    "stage_name": "GS" if action == "get_post_gs_data" else "VS",
                    "mesh_format": mesh_format,
                },
            )
        vertex_offset = int(getattr(mesh, "vertexByteOffset", 0) or 0)
        vertex_size = int(getattr(mesh, "vertexByteSize", 0) or 0)
        vertex_stride = int(getattr(mesh, "vertexByteStride", 0) or 0)
        raw = await _offload(controller.GetBufferData, vertex_resource_id, vertex_offset, vertex_size)
        mesh_format = _mesh_format_payload(mesh)
        rows = _mesh_vertex_rows(raw, stride=vertex_stride, max_vertices=max_vertices)
        return _ok(
            mesh_data={
                "mesh_format": mesh_format,
                "vertex_rows": rows,
                "vertex_count": len(rows),
                "truncated": bool(max_vertices > 0 and len(raw) // max(vertex_stride, 1) > len(rows)),
            },
            resolved_event_id=int(event_id),
        )
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

    async def read_npz(
        texture_id: Any,
        subresource: Optional[Dict[str, Any]],
        region: Optional[Dict[str, Any]],
        *,
        event_id_override: Optional[int] = None,
    ) -> Tuple[Any, Dict[str, Any], Optional[str], int]:
        requested_event = _as_int(event_id_override, 0) if event_id_override is not None else 0
        if requested_event > 0:
            event_id = await _ensure_event(session_id, requested_event)
        else:
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
        return artifact_ref, stats, _artifact_path(artifact_ref), int(event_id)

    if action in {"get_data", "get_subresource_data"}:
        _require(args, "texture_id")
        subresource = _as_dict(args.get("subresource"), default={})
        if action == "get_subresource_data":
            subresource = {
                "mip": _as_int(args.get("mip"), subresource.get("mip", 0)),
                "slice": _as_int(args.get("slice"), subresource.get("slice", 0)),
                "sample": _as_int(args.get("sample"), subresource.get("sample", 0)),
            }
        artifact_ref, stats, artifact_path, resolved_event_id = await read_npz(
            args.get("texture_id"),
            subresource,
            None,
            event_id_override=_as_int(args.get("event_id"), 0) if args.get("event_id") is not None else None,
        )
        if artifact_path is None:
            return _err("Failed to store texture data artifact")
        output_path = str(args.get("output_path") or "").strip()
        saved_path = artifact_path
        if output_path:
            out = Path(output_path)
            if out.suffix.lower() != ".npz":
                return _err(
                    "rd.texture.get_data writes numeric readback containers; output_path must use the .npz extension",
                    code="texture_output_path_extension_mismatch",
                    category="validation",
                    details={
                        "output_path": output_path,
                        "expected_extension": ".npz",
                        "container_format": "npz",
                    },
                )
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact_path, out)
            saved_path = str(out)
        result: Dict[str, Any] = {
            "artifact_path": artifact_path,
            "saved_path": saved_path,
            "content_kind": "texture_readback_container",
            "container_format": "npz",
            "stats": stats,
            "byte_size": int(getattr(artifact_ref, "bytes", 0)),
            "resolved_event_id": int(resolved_event_id),
        }
        if _as_bool(args.get("as_base64"), False):
            import base64

            payload = Path(artifact_path).read_bytes()
            result["base64"] = base64.b64encode(payload).decode("ascii")
        return _ok(**result)

    if action == "get_pixel_value":
        _require(args, "texture_id", "x", "y")
        event_id = await _ensure_event(
            session_id,
            _as_int(args.get("event_id"), 0) if args.get("event_id") is not None else None,
        )
        rid = await _resolve_texture_id(session_id, args["texture_id"], event_id=event_id)
        pixel = await _render_service.pick_pixel(
            session_id=session_id,
            event_id=event_id,
            texture_id=rid,
            x=_as_int(args["x"]),
            y=_as_int(args["y"]),
            session_manager=_session_manager,
        )
        return _ok(pixel=pixel, resolved_event_id=int(event_id))

    if action == "get_region_values":
        _require(args, "texture_id", "rect")
        rect = _as_dict(args["rect"])
        region = {"x": _as_int(rect.get("x"), 0), "y": _as_int(rect.get("y"), 0), "width": _as_int(rect.get("w"), 1), "height": _as_int(rect.get("h"), 1)}
        subresource = {
            "mip": _as_int(args.get("mip"), 0),
            "slice": _as_int(args.get("slice"), 0),
            "sample": _as_int(args.get("sample"), 0),
        }
        artifact_ref, stats, artifact_path, resolved_event_id = await read_npz(
            args["texture_id"],
            subresource,
            region,
            event_id_override=_as_int(args.get("event_id"), 0) if args.get("event_id") is not None else None,
        )
        return _ok(values_path=artifact_path, stats=stats, resolved_event_id=int(resolved_event_id))

    if action == "get_min_max":
        _require(args, "texture_id")
        event_id = await _ensure_event(
            session_id,
            _as_int(args.get("event_id"), 0) if args.get("event_id") is not None else None,
        )
        rid = await _resolve_texture_id(session_id, args["texture_id"], event_id=event_id)
        stats = await _render_service.get_texture_stats(
            session_id=session_id,
            event_id=event_id,
            texture_id=rid,
            session_manager=_session_manager,
        )
        return _ok(min_max=stats, resolved_event_id=int(event_id))

    if action == "get_histogram":
        _require(args, "texture_id")
        try:
            import numpy as np
        except Exception as exc:
            return _err(f"numpy unavailable: {exc}")
        subresource = {"mip": _as_int(args.get("mip"), 0), "slice": _as_int(args.get("slice"), 0), "sample": 0}
        artifact_ref, stats, artifact_path, resolved_event_id = await read_npz(
            args["texture_id"],
            subresource,
            None,
            event_id_override=_as_int(args.get("event_id"), 0) if args.get("event_id") is not None else None,
        )
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
        return _ok(histogram=out, resolved_event_id=int(resolved_event_id))

    if action == "get_pixel_history":
        _require(args, "texture_id", "x", "y")
        controller = await _get_controller(session_id)
        event_id = await _ensure_event(
            session_id,
            _as_int(args.get("event_id"), 0) if args.get("event_id") is not None else None,
        )
        rid = await _resolve_texture_id(session_id, args["texture_id"], event_id=event_id)
        rd = _get_rd()
        sub = rd.Subresource()
        sub.mip = _as_int(args.get("mip"), 0)
        sub.slice = _as_int(args.get("slice"), 0)
        sub.sample = _as_int(args.get("sample"), 0)
        try:
            history_raw = await _pixel_history_raw_with_timeout(
                controller,
                rid,
                _as_int(args["x"]),
                _as_int(args["y"]),
                sub,
            )
        except asyncio.TimeoutError:
            return _err(
                _pixel_history_timeout_message(PIXEL_HISTORY_TIMEOUT_S),
                code="pixel_history_timeout",
                category="runtime",
                details={
                    "session_id": session_id,
                    "texture_id": str(rid),
                    "x": _as_int(args["x"]),
                    "y": _as_int(args["y"]),
                    "timeout_seconds": float(PIXEL_HISTORY_TIMEOUT_S),
                    "resolved_event_id": int(event_id),
                },
            )
        except Exception as exc:
            return _err(f"PixelHistory unavailable: {exc}")
        history = [_pixel_history_item_payload(item) for item in history_raw]
        return _ok(history=history, resolved_event_id=int(event_id))

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
            art_a, _, path_a, _ = await read_npz(tex_a.get("texture_id"), _as_dict(tex_a.get("subresource"), default={}), None)
            art_b, _, path_b, _ = await read_npz(tex_b.get("texture_id"), _as_dict(tex_b.get("subresource"), default={}), None)
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
        _, stats, _, resolved_event_id = await read_npz(
            args["texture_id"],
            {"mip": _as_int(args.get("mip"), 0), "slice": _as_int(args.get("slice"), 0), "sample": _as_int(args.get("sample"), 0)},
            None,
            event_id_override=_as_int(args.get("event_id"), 0) if args.get("event_id") is not None else None,
        )
        return _ok(
            stats={**dict(stats or {}), "event_id": int(resolved_event_id)},
            resolved_event_id=int(resolved_event_id),
        )

    return _err(f"Unsupported texture action: {action}")


def _shader_encoding_name(value: Any) -> str:
    rd = _get_rd()
    try:
        raw_value = int(value)
    except Exception:
        raw_value = None
    if raw_value is not None:
        for name in dir(rd.ShaderEncoding):
            if name.startswith("_"):
                continue
            try:
                if int(getattr(rd.ShaderEncoding, name)) != raw_value:
                    continue
            except Exception:
                continue
            mapping = {
                "SPIRVAsm": "spirvasm",
                "SPIRV": "spirv",
                "DXBC": "dxbc",
                "DXIL": "dxil",
                "HLSL": "hlsl",
                "GLSL": "glsl",
                "Slang": "slang",
            }
            return mapping.get(name, str(name).strip().lower())
    text = str(value or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def _shader_encoding_from_name(name: str) -> Any:
    rd = _get_rd()
    normalized = str(name or "").strip().lower()
    mapping = {
        "hlsl": getattr(rd.ShaderEncoding, "HLSL", None),
        "glsl": getattr(rd.ShaderEncoding, "GLSL", None),
        "spirv": getattr(rd.ShaderEncoding, "SPIRV", None),
        "spirvasm": getattr(rd.ShaderEncoding, "SPIRVAsm", None),
        "spirv_asm": getattr(rd.ShaderEncoding, "SPIRVAsm", None),
        "dxbc": getattr(rd.ShaderEncoding, "DXBC", None),
        "dxil": getattr(rd.ShaderEncoding, "DXIL", None),
        "slang": getattr(rd.ShaderEncoding, "Slang", None),
    }
    return mapping.get(normalized)


def _shader_binary_suffix(container: str) -> str:
    normalized = str(container or "").strip().lower()
    mapping = {
        "dxbc": ".dxbc",
        "dxil": ".dxil",
        "spirv": ".spv",
        "spirvasm": ".spvasm",
        "spirv_asm": ".spvasm",
        "glsl": ".glsl",
        "hlsl": ".hlslbin",
    }
    return mapping.get(normalized, ".bin")


def _shader_raw_bytes(reflection: Any) -> bytes:
    raw_bytes = getattr(reflection, "rawBytes", None)
    if raw_bytes is None:
        return b""
    try:
        return bytes(raw_bytes)
    except Exception:
        pass
    try:
        return raw_bytes.tobytes()
    except Exception:
        return b""


async def _dispatch_shader(action: str, args: Dict[str, Any]) -> str:
    if action == "compile":
        session_id = str(args.get("session_id") or _context_snapshot().get("runtime", {}).get("session_id") or "").strip()
        if not session_id:
            return _err(
                "rd.shader.compile requires session_id or a current context session",
                code="validation_error",
                category="validation",
            )
        controller = await _get_controller(session_id)
        rd = _get_rd()
        source = str(args.get("source") or "")
        if not source:
            return _err("Missing required parameter(s): source", code="validation_error", category="validation")
        stage = _parse_stage(args.get("stage"))
        entry = str(args.get("entry") or "main").strip() or "main"
        requested_encoding_name = str(args.get("source_encoding") or "").strip().lower()
        target_hint = str(args.get("target") or "").strip().lower()
        if not requested_encoding_name:
            if "#version" in source.lower():
                requested_encoding_name = "glsl"
            elif target_hint.startswith("spirv") or source.lstrip().startswith("Op"):
                requested_encoding_name = "spirvasm"
            else:
                requested_encoding_name = "hlsl"
        requested_encoding = _shader_encoding_from_name(requested_encoding_name)
        supported_encodings = list(await _offload(controller.GetTargetShaderEncodings) or [])
        supported_encoding_names = [_shader_encoding_name(item) for item in supported_encodings]
        if requested_encoding is None:
            return _err(
                f"Unsupported shader source encoding: {requested_encoding_name}",
                code="shader_compile_encoding_unsupported",
                category="validation",
                details={"requested_source_encoding": requested_encoding_name, "supported_source_encodings": supported_encoding_names},
            )
        if requested_encoding not in supported_encodings:
            return _err(
                f"Requested shader source encoding is unsupported for this session: {requested_encoding_name}",
                code="shader_compile_encoding_unsupported",
                category="validation",
                details={"requested_source_encoding": requested_encoding_name, "supported_source_encodings": supported_encoding_names},
            )
        compile_flags = rd.ShaderCompileFlags()
        shader_id, messages = await _offload(
            controller.BuildTargetShader,
            entry,
            requested_encoding,
            source.encode("utf-8"),
            compile_flags,
            _rd_stage(stage),
        )
        if _is_null_resource_id(shader_id):
            return _err(
                str(messages or "BuildTargetShader returned a null shader id"),
                code="shader_compile_failed",
                category="runtime",
                details={
                    "session_id": session_id,
                    "stage": stage.upper(),
                    "entry": entry,
                    "requested_source_encoding": requested_encoding_name,
                    "supported_source_encodings": supported_encoding_names,
                },
            )
        result: Dict[str, Any] = {
            "session_id": session_id,
            "shader_id": str(shader_id),
            "entry": entry,
            "stage": stage.upper(),
            "source_encoding": requested_encoding_name,
            "supported_source_encodings": supported_encoding_names,
            "messages": str(messages or ""),
            "compiler_messages": str(messages or ""),
        }
        try:
            entry_points = await _offload(controller.GetShaderEntryPoints, shader_id)
            if entry_points:
                reflection = await _offload(controller.GetShader, rd.ResourceId(), shader_id, entry_points[0])
                raw_bytes = _shader_raw_bytes(reflection)
            else:
                raw_bytes = b""
        except Exception:
            raw_bytes = b""
        if raw_bytes:
            output_path = args.get("output_path")
            suffix = _shader_binary_suffix(requested_encoding_name)
            if output_path:
                out = Path(str(output_path))
                if out.suffix == "":
                    out = out.with_suffix(suffix)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(raw_bytes)
                result["saved_path"] = str(out)
            else:
                assert _artifact_store is not None
                artifact = await _artifact_store.store(raw_bytes, mime="application/octet-stream", suffix=suffix)
                result["artifact_path"] = _artifact_path(artifact)
            result["byte_size"] = len(raw_bytes)
        return _ok(**result)

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

        x = _as_int(params.get("x"), 0)
        y = _as_int(params.get("y"), 0)
        target = _parse_target_like(params.get("target"))
        sample_raw = params.get("sample")
        view_raw = params.get("view")
        primitive_raw = params.get("primitive")
        sample_override = _as_int(sample_raw, 0) if sample_raw is not None else None
        view_override = _as_int(view_raw, 0) if view_raw is not None else None
        primitive_override = _as_int(primitive_raw, -1) if primitive_raw is not None else None
        if primitive_override is not None and primitive_override < 0:
            primitive_override = None

        rd = _get_rd()
        target_candidates: List[Tuple[str, Dict[str, Any]]] = []
        seen_target_candidates: set[str] = set()

        def add_target_candidate(source_label: str, target_value: Dict[str, Any]) -> None:
            normalized = dict(target_value or {})
            key = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
            if key in seen_target_candidates:
                return
            seen_target_candidates.add(key)
            target_candidates.append((source_label, normalized))

        if target:
            add_target_candidate("user.target", target)
        else:
            add_target_candidate("default_target", {})
        try:
            for rid, rt_index in await _output_target_resource_ids(session_id, event_id):
                add_target_candidate(
                    f"event.output[{rt_index}]",
                    {"rt_index": int(rt_index), "texture_id": str(rid)},
                )
        except Exception:
            pass

        trace = None
        attempts_log: List[Dict[str, Any]] = []
        last_context: Dict[str, Any] = {
            "event_id": int(event_id),
            "x": int(x),
            "y": int(y),
            "target": dict(target or {}),
        }
        last_target_source = ""
        last_history_summary: Dict[str, Any] = {
            "hit_count": 0,
            "matched_event_hit_count": 0,
            "passed_hit_count": 0,
            "viable_hit_count": 0,
            "primitive_ids": [],
        }
        target_config_failures = 0
        configured_target_count = 0
        debug_attempt_count = 0
        debug_exception_count = 0
        invalid_trace_count = 0
        cross_event_rejections = 0
        debugger_missing_count = 0
        matched_event_hit_seen = False
        pixel_history_timeout_count = 0

        for target_source, target_candidate in target_candidates:
            try:
                target_rid, _, target_sub = await _configure_texture_output_for_target(
                    session_id,
                    target_candidate,
                    event_id=event_id,
                    sample_override=sample_override,
                )
                await _refresh_pixel_context(session_id, x, y)
            except Exception as exc:
                target_config_failures += 1
                attempts_log.append(
                    {
                        "target_source": target_source,
                        "stage": "configure_target",
                        "resolved_context": {
                            "event_id": int(event_id),
                            "x": int(x),
                            "y": int(y),
                            "target": dict(target_candidate),
                        },
                        "error": f"Failed to configure debug target: {exc}",
                        "exception_type": type(exc).__name__,
                    },
                )
                continue

            configured_target_count += 1
            resolved_target = {
                "texture_id": str(target_rid),
                "subresource": _subresource_to_dict(target_sub),
            }
            if "rt_index" in target_candidate:
                resolved_target["rt_index"] = _as_int(target_candidate.get("rt_index"), 0)
            last_context = {
                "event_id": int(event_id),
                "x": int(x),
                "y": int(y),
                "target": resolved_target,
            }
            last_target_source = target_source
            history_timed_out = False

            try:
                history_raw = await _pixel_history_raw_with_timeout(
                    controller,
                    target_rid,
                    x,
                    y,
                    target_sub,
                )
            except asyncio.TimeoutError:
                pixel_history_timeout_count += 1
                history_items = []
                history_summary = {
                    "hit_count": 0,
                    "matched_event_hit_count": 0,
                    "passed_hit_count": 0,
                    "viable_hit_count": 0,
                    "primitive_ids": [],
                    "error": _pixel_history_timeout_message(PIXEL_HISTORY_TIMEOUT_S),
                    "timeout_seconds": float(PIXEL_HISTORY_TIMEOUT_S),
                }
                history_timed_out = True
                attempts_log.append(
                    {
                        "target_source": target_source,
                        "stage": "pixel_history",
                        "resolved_context": {
                            "event_id": int(event_id),
                            "x": int(x),
                            "y": int(y),
                            "target": resolved_target,
                        },
                        "error": _pixel_history_timeout_message(PIXEL_HISTORY_TIMEOUT_S),
                        "timeout_seconds": float(PIXEL_HISTORY_TIMEOUT_S),
                    },
                )
            except Exception as exc:
                history_items: List[Dict[str, Any]] = []
                history_summary = {
                    "hit_count": 0,
                    "matched_event_hit_count": 0,
                    "passed_hit_count": 0,
                    "viable_hit_count": 0,
                    "primitive_ids": [],
                    "error": str(exc),
                }
            else:
                history_items = [_pixel_history_item_payload(item) for item in history_raw]
                history_summary = _pixel_history_summary(history_items, event_id)

            history_summary["target_source"] = target_source
            history_summary["target"] = resolved_target
            last_history_summary = history_summary
            if int(history_summary.get("matched_event_hit_count", 0)) > 0:
                matched_event_hit_seen = True
            if history_timed_out:
                continue
            default_sample = sample_override if sample_override is not None else _subresource_to_dict(target_sub)["sample"]
            default_view = view_override if view_override is not None else 0

            attempts: List[Tuple[str, str, int, Optional[int], Optional[int], Optional[int]]] = []
            seen_attempts: set[Tuple[str, int, Optional[int], Optional[int], Optional[int]]] = set()

            def add_attempt(
                label: str,
                origin: str,
                event_value: int,
                sample_value: Optional[int],
                view_value: Optional[int],
                primitive_value: Optional[int],
            ) -> None:
                key = (str(target_rid), int(event_value), sample_value, view_value, primitive_value)
                if key in seen_attempts:
                    return
                seen_attempts.add(key)
                attempts.append((label, origin, int(event_value), sample_value, view_value, primitive_value))

            add_attempt("user_context", "explicit", event_id, sample_override, view_override, primitive_override)
            add_attempt("default_context", "default", event_id, default_sample, default_view, primitive_override)

            for item in history_items:
                if not bool(item.get("passed")):
                    continue
                if bool(item.get("shader_discarded")) or bool(item.get("unbound_ps")):
                    continue
                candidate_event = int(item.get("event_id") or 0)
                if candidate_event <= 0:
                    continue
                if candidate_event != int(event_id):
                    cross_event_rejections += 1
                    attempts_log.append(
                        {
                            "target_source": target_source,
                            "label": "pixel_history_match",
                            "origin": "pixel_history",
                            "event_id": int(candidate_event),
                            "error": "Cross-event shader debug fallback is not allowed for rd.shader.debug_start",
                        },
                    )
                    continue
                raw_primitive = item.get("primitive_id")
                candidate_primitive = int(raw_primitive) if raw_primitive is not None else -1
                if candidate_primitive < 0:
                    continue
                add_attempt(
                    "pixel_history_match",
                    "pixel_history",
                    candidate_event,
                    default_sample,
                    default_view,
                    candidate_primitive,
                )

            for label, origin, attempt_event, attempt_sample, attempt_view, attempt_primitive in attempts:
                if int(attempt_event) != int(last_context.get("event_id", event_id)):
                    cross_event_rejections += 1
                    attempts_log.append(
                        {
                            "target_source": target_source,
                            "label": label,
                            "origin": origin,
                            "event_id": int(attempt_event),
                            "error": "Cross-event shader debug fallback is not supported",
                        },
                    )
                    continue
                inputs = rd.DebugPixelInputs()
                if attempt_sample is not None:
                    inputs.sample = int(attempt_sample)
                if attempt_view is not None:
                    inputs.view = int(attempt_view)
                if attempt_primitive is not None:
                    inputs.primitive = int(attempt_primitive)
                debug_attempt_count += 1
                try:
                    trace = await _offload(controller.DebugPixel, x, y, inputs)
                except Exception as exc:
                    debug_exception_count += 1
                    attempts_log.append(
                        {
                            "target_source": target_source,
                            "label": label,
                            "origin": origin,
                            "event_id": int(attempt_event),
                            "resolved_context": {
                                "event_id": int(attempt_event),
                                "x": int(x),
                                "y": int(y),
                                "sample": int(attempt_sample if attempt_sample is not None else resolved_target["subresource"]["sample"]),
                                "view": int(attempt_view if attempt_view is not None else 0),
                                "primitive": int(attempt_primitive) if attempt_primitive is not None else None,
                                "target": resolved_target,
                            },
                            "pixel_history_hit_count": int(history_summary.get("hit_count", 0)),
                            "matched_event_hit_count": int(history_summary.get("matched_event_hit_count", 0)),
                            "stage": "debug_pixel",
                            "error": f"DebugPixel failed: {exc}",
                            "exception_type": type(exc).__name__,
                        },
                    )
                    continue
                effective_context = {
                    "event_id": int(attempt_event),
                    "x": int(x),
                    "y": int(y),
                    "sample": int(attempt_sample if attempt_sample is not None else resolved_target["subresource"]["sample"]),
                    "view": int(attempt_view if attempt_view is not None else 0),
                    "primitive": int(attempt_primitive) if attempt_primitive is not None else None,
                    "target": resolved_target,
                }
                valid = bool(trace is not None and getattr(trace, "valid", False))
                missing_debugger_handle = bool(
                    trace is not None and getattr(trace, "debugger", None) is None
                )
                if missing_debugger_handle:
                    debugger_missing_count += 1
                if valid and missing_debugger_handle:
                    valid = False
                attempts_log.append(
                    {
                        "target_source": target_source,
                        "label": label,
                        "origin": origin,
                        "event_id": int(attempt_event),
                        "resolved_context": effective_context,
                        "pixel_history_hit_count": int(history_summary.get("hit_count", 0)),
                        "matched_event_hit_count": int(history_summary.get("matched_event_hit_count", 0)),
                        "trace_valid": valid,
                        "stage": "debug_pixel",
                    },
                )
                if not valid and missing_debugger_handle:
                    attempts_log[-1]["failure_stage"] = "trace_state"
                    attempts_log[-1]["error"] = "Debug trace was created without a debugger handle"
                if not valid and trace is None:
                    invalid_trace_count += 1
                last_context = effective_context
                if valid:
                    break
                if trace is not None:
                    invalid_trace_count += 1
                    try:
                        await _offload(controller.FreeTrace, trace)
                    except Exception:
                        pass
                    trace = None
            if trace is not None and getattr(trace, "valid", False):
                break

        if trace is None or not getattr(trace, "valid", False):
            failure_stage = "debug_pixel"
            failure_reason = "invalid_trace"
            error_code = "shader_debug_event_binding_unavailable"
            error_category = "capability"
            error_message = "Precise event-bound shader debug is unavailable for the requested event"

            if configured_target_count == 0 and target_config_failures > 0:
                failure_stage = "configure_target"
                failure_reason = "all_targets_failed"
                error_code = "shader_debug_target_config_failed"
                error_category = "runtime"
                error_message = "Failed to configure any shader debug target for the requested event"
            elif debug_exception_count > 0 and debug_attempt_count == debug_exception_count:
                failure_stage = "debug_pixel"
                failure_reason = "debug_pixel_exception"
                error_code = "shader_debug_start_failed"
                error_category = "runtime"
                error_message = "Shader debug startup failed while requesting the backend trace"
            elif debugger_missing_count > 0:
                failure_stage = "trace_state"
                failure_reason = "debugger_handle_missing"
                error_code = "shader_debug_start_failed"
                error_category = "runtime"
                error_message = "Shader debug trace was created without a usable debugger handle"
            elif pixel_history_timeout_count > 0 and debug_attempt_count == 0:
                failure_stage = "pixel_history"
                failure_reason = "pixel_history_timeout"
                error_code = "shader_debug_start_failed"
                error_category = "runtime"
                error_message = "Shader debug target discovery timed out while collecting pixel history"
            elif not matched_event_hit_seen and cross_event_rejections > 0:
                failure_stage = "pixel_history"
                failure_reason = "cross_event_only"
            elif invalid_trace_count > 0:
                failure_stage = "debug_pixel"
                failure_reason = "invalid_trace"

            details = {
                "resolved_context": last_context,
                "pixel_history_summary": last_history_summary,
                "attempts": attempts_log,
                "selected_target_source": last_target_source,
                "failure_stage": failure_stage,
                "failure_reason": failure_reason,
                "target_config_failures": int(target_config_failures),
                "configured_target_count": int(configured_target_count),
                "debug_attempt_count": int(debug_attempt_count),
                "debug_exception_count": int(debug_exception_count),
                "invalid_trace_count": int(invalid_trace_count),
                "cross_event_rejections": int(cross_event_rejections),
                "matched_event_hit_seen": bool(matched_event_hit_seen),
                "debugger_missing_count": int(debugger_missing_count),
                "pixel_history_timeout_count": int(pixel_history_timeout_count),
            }
            if error_category == "runtime":
                return _err(
                    error_message,
                    code=error_code,
                    category=error_category,
                    details=details,
                )
            return _capability_error(
                error_code,
                error_message,
                capability="shader_debug",
                reason="The replay backend could not provide a valid debug trace without leaving the requested event.",
                source="renderdoc_runtime",
                action=action,
                **details,
            )
        shader_debug_id = _new_id("sdbg")
        resolved_context = dict(last_context)
        resolved_event_id = int(resolved_context.get("event_id") or event_id)
        _runtime.shader_debugs[shader_debug_id] = ShaderDebugHandle(
            shader_debug_id=shader_debug_id,
            session_id=session_id,
            mode=mode,
            event_id=resolved_event_id,
            trace=trace,
            debugger=getattr(trace, "debugger", None),
            current_state=None,
            resolved_context=resolved_context,
            selected_target_source=last_target_source,
            pixel_history_summary=dict(last_history_summary),
        )
        return _ok(
            shader_debug_id=shader_debug_id,
            initial_state={"pc": 0},
            resolved_context=resolved_context,
            resolved_event_id=resolved_event_id,
            selected_target_source=last_target_source,
            pixel_history_summary=last_history_summary,
        )

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
        return _ok(
            state=payload,
            resolved_event_id=int(handle.event_id or 0),
            resolved_context=handle.resolved_context,
            selected_target_source=handle.selected_target_source,
            pixel_history_summary=handle.pixel_history_summary,
        )

    if action == "list_replacements":
        replacements = _runtime.shader_replacements.get(session_id, [])
        return _ok(replacements=replacements)

    if action == "revert_replacement":
        _require(args, "replacement_id")
        replacement_id = str(args["replacement_id"])
        repl = _runtime.shader_replacements.get(session_id, [])
        if _patch_engine is None:
            return _err("Shader patch engine is not initialized", code="shader_replace_unavailable", category="runtime")
        reverted = await _patch_engine.revert_patch(session_id, replacement_id, _session_manager)
        if not reverted:
            return _err(
                f"Unknown replacement_id: {replacement_id}",
                code="replacement_not_found",
                category="not_found",
                details={"replacement_id": replacement_id, "session_id": session_id},
            )
        remaining_replacements = [
            r for r in repl if str(r.get("replacement_id")) != replacement_id
        ]
        _runtime.shader_replacements[session_id] = remaining_replacements
        try:
            await _maybe_refresh_remote_session_after_revert(
                session_id,
                remaining_replacements=remaining_replacements,
            )
        except Exception as exc:
            return _err(
                "Replacement reverted but replay session recovery failed",
                code="replacement_revert_recovery_failed",
                category="runtime",
                details={
                    "replacement_id": replacement_id,
                    "session_id": session_id,
                    "remaining_replacements": len(remaining_replacements),
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                },
            )
        return _ok(replacement_id=replacement_id, reverted=True)

    if action == "edit_and_replace":
        stage = _parse_stage(args.get("stage"))
        if _patch_engine is None:
            return _capability_error(
                "shader_replace_unavailable",
                "Runtime shader replacement is not initialized",
                capability="shader_replace",
                reason="Shader patch engine is not initialized.",
                source="runtime_build",
                action=action,
            )
        patch_supported, patch_reason = _session_replace_capability(session_id, controller)
        if not patch_supported:
            return _capability_error(
                "shader_replace_backend_unsupported",
                "Runtime shader replacement is unavailable for this replay backend",
                capability="shader_replace",
                reason=patch_reason,
                source="renderdoc_runtime",
                action=action,
                session_id=session_id,
                resolved_event_id=int(event_id),
            )
        ops_payload = _as_list(args.get("ops"), default=[])
        source_text = str(args.get("source_text") or "")
        diff_text = str(args.get("diff_text") or "")
        edit_input_count = sum(
            1
            for item in (
                bool(ops_payload),
                bool(source_text),
                bool(diff_text),
            )
            if item
        )
        if edit_input_count != 1:
            return _err(
                "rd.shader.edit_and_replace requires exactly one edit input: ops, source_text, or diff_text",
                code="validation_error",
                category="validation",
            )
        shader_id = str(args.get("shader_id", "")).strip()
        if shader_id:
            bound_stage = await _find_stage_by_shader(shader_id)
            if bound_stage is None:
                return _err(
                    f"Shader not bound at current event: {shader_id}",
                    code="shader_not_bound",
                    category="runtime",
                    details={"session_id": session_id, "resolved_event_id": int(event_id), "shader_id": shader_id},
                )
            if bound_stage != stage:
                return _err(
                    f"Shader stage mismatch for {shader_id}: expected {stage}, got {bound_stage}",
                    code="shader_stage_mismatch",
                    category="validation",
                    details={"session_id": session_id, "resolved_event_id": int(event_id), "shader_id": shader_id, "expected_stage": stage, "bound_stage": bound_stage},
                )
        else:
            rd_stage = _rd_stage(stage)
            shader_obj = await _offload(pipe.GetShader, rd_stage)
            if _is_null_resource_id(shader_obj):
                return _err(
                    f"No shader bound at stage {stage.upper()} for event {int(event_id)}",
                    code="shader_not_bound",
                    category="runtime",
                    details={"session_id": session_id, "resolved_event_id": int(event_id), "stage": stage.upper()},
                )
            shader_id = str(shader_obj)
        patch_spec = PatchSpec.model_validate(
            {
                "patch_id": str(args.get("replacement_id") or _new_id("repl")),
                "target_event_id": int(event_id),
                "target_stage": ShaderStage(stage),
                "target_shader_id": shader_id,
                "intent": str(args.get("intent") or "shader_replace"),
                "ops": ops_payload,
                "source_text": source_text,
                "diff_text": diff_text,
                "source_target": str(args.get("source_target") or ""),
                "source_encoding": str(args.get("source_encoding") or ""),
                "expected_source_hash": str(args.get("expected_source_hash") or ""),
                "max_diff_ops": _as_int(args.get("max_diff_ops"), 20),
                "preserve_outputs": _as_bool(args.get("preserve_outputs"), True),
            }
        )
        emit_patch_artifacts = _as_bool(args.get("emit_patch_artifacts"), False)
        patch_output_dir = str(args.get("output_dir") or "").strip()
        patch_result = await _patch_engine.apply_patch(
            session_id=session_id,
            event_id=int(event_id),
            stage=ShaderStage(stage),
            session_manager=_session_manager,
            patch_spec=patch_spec,
        )
        if not patch_result.success:
            error_details = {
                "session_id": session_id,
                "resolved_event_id": int(event_id),
                "stage": stage.upper(),
                "shader_id": shader_id,
                "replacement_id": patch_spec.patch_id,
            }
            if patch_result.error_details:
                error_details.update(dict(patch_result.error_details))
            return _err(
                patch_result.error_message or "Shader replacement failed",
                code=patch_result.error_code or "shader_replace_failed",
                category=patch_result.error_category or "runtime",
                details=error_details,
            )
        no_source_change = bool(
            patch_result.applied_to_shader_hash
            and patch_result.applied_to_shader_hash == patch_result.original_shader_hash
            and any(
                "no source changes before recompilation" in str(msg).lower()
                for msg in (patch_result.messages or [])
            )
        )
        replacement = {
            "replacement_id": patch_spec.patch_id,
            "stage": stage.upper(),
            "resolved_event_id": int(event_id),
            "original_shader_id": shader_id,
            "status": "noop" if no_source_change else "applied",
            "messages": list(patch_result.messages or []),
            "applied_to_shader_hash": patch_result.applied_to_shader_hash,
            "original_shader_hash": patch_result.original_shader_hash,
            "compile": {
                "encoding": str(patch_result.encoding or ""),
                "disassembly_target": str(patch_result.disassembly_target or ""),
                "entry_point": str(patch_result.entry_point or ""),
                "compile_flags": list(patch_result.compile_flags or []),
            },
        }
        artifacts: List[Dict[str, Any]] = []
        if emit_patch_artifacts or patch_output_dir:
            base_stem = f"shader_patch_ev{int(event_id)}_{stage}_{patch_spec.patch_id}"
            before_payload = await _store_text_artifact_payload(
                str(patch_result.source_before_text or ""),
                stem=f"{base_stem}_before",
                suffix=".txt",
                output_dir=patch_output_dir,
                title="shader_source_before",
            )
            after_payload = await _store_text_artifact_payload(
                str(patch_result.source_after_text or ""),
                stem=f"{base_stem}_after",
                suffix=".txt",
                output_dir=patch_output_dir,
                title="shader_source_after",
            )
            diff_text = ""
            if patch_result.source_before_text or patch_result.source_after_text:
                diff_text = "".join(
                    difflib.unified_diff(
                        str(patch_result.source_before_text or "").splitlines(keepends=True),
                        str(patch_result.source_after_text or "").splitlines(keepends=True),
                        fromfile="before",
                        tofile="after",
                    )
                )
            diff_payload = await _store_text_artifact_payload(
                diff_text,
                stem=f"{base_stem}_diff",
                suffix=".diff",
                output_dir=patch_output_dir,
                title="shader_patch_diff",
                mime="text/x-diff",
            )
            patch_artifacts = {
                "source_before": before_payload,
                "source_after": after_payload,
                "patch_diff": diff_payload,
            }
            replacement["artifacts"] = {
                key: value
                for key, value in patch_artifacts.items()
                if value
            }
            artifacts = [value for value in patch_artifacts.values() if value]
        if not no_source_change:
            _runtime.shader_replacements.setdefault(session_id, []).append(replacement)
        return _ok(
            replacement_id=replacement["replacement_id"],
            status=replacement["status"],
            resolved_event_id=int(event_id),
            messages=replacement["messages"],
            replacement=replacement,
            artifacts=artifacts,
        )

    if action == "get_messages":
        severity_min = str(args.get("severity_min", "info"))
        replacements = _runtime.shader_replacements.get(session_id, [])
        messages = []
        for r in replacements:
            for msg in r.get("messages", []):
                messages.append({"severity": "info", "message": msg})
        return _ok(messages=messages, severity_min=severity_min)

    if action == "save_binary":
        action = "extract_binary"
    if action == "extract_binary":
        shader_id = str(args.get("shader_id", "")).strip()
        stage = _parse_stage(args.get("stage"))
        if shader_id:
            stage = await _find_stage_by_shader(shader_id)
            if stage is None:
                return _err(
                    f"Shader not bound at current event: {shader_id}",
                    code="shader_not_bound",
                    category="runtime",
                    details={"session_id": session_id, "resolved_event_id": int(event_id), "shader_id": shader_id},
                )
        else:
            rd_stage_explicit = _rd_stage(stage)
            bound_shader = await _offload(pipe.GetShader, rd_stage_explicit)
            if _is_null_resource_id(bound_shader):
                return _err(
                    f"No shader bound at stage {stage.upper()} for event {int(event_id)}",
                    code="shader_not_bound",
                    category="runtime",
                    details={"session_id": session_id, "resolved_event_id": int(event_id), "stage": stage.upper()},
                )
            shader_id = str(bound_shader)
        rd_stage = _rd_stage(stage)
        reflection = await _offload(pipe.GetShaderReflection, rd_stage)
        raw_bytes = _shader_raw_bytes(reflection)
        if not raw_bytes:
            return _capability_error(
                "shader_binary_export_unavailable",
                "Shader reflection does not expose raw bytes for the requested shader",
                capability="shader_binary_export",
                reason="Shader reflection does not expose raw bytes for the requested shader.",
                source="renderdoc_runtime",
                action=action,
                session_id=session_id,
                shader_id=shader_id,
                resolved_event_id=int(event_id),
            )
        encoding_name = _shader_encoding_name(getattr(reflection, "encoding", ""))
        container = str(args.get("container") or encoding_name or "bin").strip().lower() or "bin"
        result: Dict[str, Any] = {
            "shader_id": shader_id,
            "encoding": encoding_name,
            "container": container,
            "byte_size": len(raw_bytes),
            "resolved_event_id": int(event_id),
        }
        output_path = args.get("output_path")
        suffix = _shader_binary_suffix(container or encoding_name)
        if output_path:
            out = Path(str(output_path))
            if out.suffix == "":
                out = out.with_suffix(suffix)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw_bytes)
            result["saved_path"] = str(out)
        else:
            assert _artifact_store is not None
            artifact = await _artifact_store.store(raw_bytes, mime="application/octet-stream", suffix=suffix)
            result["artifact_path"] = _artifact_path(artifact)
        if _as_bool(args.get("as_base64"), False):
            import base64

            result["base64"] = base64.b64encode(raw_bytes).decode("ascii")
        return _ok(**result)
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
        shader_id = str(args.get("shader_id", "")).strip()
        stage = _parse_stage(args.get("stage"))
        if shader_id:
            stage = await _find_stage_by_shader(shader_id)
            if stage is None:
                return _err(
                    f"Shader not bound at current event: {shader_id}",
                    code="shader_not_bound",
                    category="runtime",
                    details={"session_id": session_id, "resolved_event_id": int(event_id), "shader_id": shader_id},
                )
        else:
            rd_stage_explicit = _rd_stage(stage)
            bound_shader = await _offload(pipe.GetShader, rd_stage_explicit)
            if _is_null_resource_id(bound_shader):
                return _err(
                    f"No shader bound at stage {stage.upper()} for event {int(event_id)}",
                    code="shader_not_bound",
                    category="runtime",
                    details={"session_id": session_id, "resolved_event_id": int(event_id), "stage": stage.upper()},
                )
            shader_id = str(bound_shader)
        rd_stage = _rd_stage(stage)
        reflection = await _offload(pipe.GetShaderReflection, rd_stage)
        if action == "list_entry_points":
            entries = []
            if reflection is not None:
                entries.append({"name": str(getattr(reflection, "entryPoint", "main")), "stage": stage.upper()})
            return _ok(entry_points=entries, shader_id=shader_id, resolved_event_id=int(event_id))
        if action == "get_bindpoint_mapping":
            mapping = []
            if reflection is not None:
                for ro in getattr(reflection, "readOnlyResources", []) or []:
                    mapping.append({"resource_name": str(getattr(ro, "name", "")), "bindpoint": int(getattr(ro, "bindPoint", 0)), "type": "SRV", "stage": stage.upper()})
                for rw in getattr(reflection, "readWriteResources", []) or []:
                    mapping.append({"resource_name": str(getattr(rw, "name", "")), "bindpoint": int(getattr(rw, "bindPoint", 0)), "type": "UAV", "stage": stage.upper()})
            return _ok(mapping=mapping, shader_id=shader_id, resolved_event_id=int(event_id))
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
                return _ok(layout={}, shader_id=shader_id, resolved_event_id=int(event_id))
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
            return _ok(layout=layout, shader_id=shader_id, resolved_event_id=int(event_id))
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
            return _ok(reflection=refl, shader_id=shader_id, resolved_event_id=int(event_id))
        if action == "get_disassembly":
            requested_target = str(args.get("target", "auto"))
            requested_encoding = str(args.get("source_encoding") or "")
            try:
                text, resolved_encoding, resolved_target, is_raw_spirv_asm = await _offload(
                    PatchEngine._resolve_source,
                    controller,
                    pipe,
                    reflection,
                    ShaderStage(stage),
                    session_id,
                    requested_target=requested_target if requested_target != "auto" else "",
                    requested_encoding=requested_encoding,
                )
            except RuntimeError as exc:
                return _err(
                    str(exc),
                    code="shader_disassembly_unavailable",
                    category="runtime",
                    details={
                        "session_id": session_id,
                        "resolved_event_id": int(event_id),
                        "shader_id": shader_id,
                        "requested_target": requested_target,
                        "requested_source_encoding": requested_encoding,
                    },
                )
            source_encoding_name = _shader_encoding_name(resolved_encoding)
            if PatchEngine._is_raw_spirv_asm_request(requested_target, requested_encoding) or bool(is_raw_spirv_asm):
                source_encoding_name = "spirvasm"
            if not text:
                return _ok(
                    disassembly="",
                    target=str(resolved_target or ""),
                    source_encoding=source_encoding_name,
                    is_raw_spirv_asm=bool(is_raw_spirv_asm),
                    shader_id=shader_id,
                    resolved_event_id=int(event_id),
                    source_hash="",
                )
            return _ok(
                disassembly=str(text),
                target=str(resolved_target or ""),
                source_encoding=source_encoding_name,
                is_raw_spirv_asm=bool(is_raw_spirv_asm),
                shader_id=shader_id,
                resolved_event_id=int(event_id),
                source_hash=hashlib.sha256(str(text).encode("utf-8")).hexdigest(),
            )

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
        if handle.synthetic:
            next_index = min(handle.synthetic_index + 1, max(len(handle.synthetic_states) - 1, 0))
            if next_index == handle.synthetic_index and handle.current_state is not None and handle.synthetic_index >= max(len(handle.synthetic_states) - 1, 0):
                return None
            handle.synthetic_index = next_index
            handle.current_state = handle.synthetic_states[next_index] if handle.synthetic_states else None
            return handle.current_state
        states = await _offload(controller.ContinueDebug, handle.debugger)
        if not states:
            return None
        handle.current_state = states[-1]
        return handle.current_state

    if action == "step":
        if handle.synthetic and handle.current_state is None and handle.synthetic_states:
            handle.current_state = handle.synthetic_states[0]
            return _ok(state={"pc": int(getattr(handle.current_state, "stepIndex", 0))})
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
        if handle.current_state is None and int(target_pc) == 0:
            return _ok(state={"pc": 0})
        if handle.current_state is not None:
            current_pc = int(getattr(handle.current_state, "stepIndex", 0))
            if current_pc == int(target_pc):
                return _ok(state={"pc": current_pc})
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
        try:
            literal = json.loads(expr)
        except Exception:
            literal = None
        else:
            return _ok(value=literal)
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
            if handle.trace is not None:
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
        cid = _as_int(args["counter_id"])
        controller = await _get_controller(session_id)
        try:
            desc = await _offload(controller.DescribeCounter, cid)
        except Exception as exc:
            return _err(
                f"Counter not found: {cid}",
                code="counter_not_found",
                category="runtime",
                details={"counter_id": cid, "session_id": session_id, "error": str(exc)},
            )
        counter = {
            "counter_id": int(cid),
            "name": str(getattr(desc, "name", "")),
            "description": str(getattr(desc, "description", "")),
            "unit": str(getattr(desc, "unit", "")),
            "result_type": str(getattr(desc, "resultType", "")),
        }
        return _ok(counter=counter)
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
        return await _export_texture_file({"session_id": session_id, **dict(args or {})})
    if action == "buffer":
        return await _export_buffer_file({"session_id": session_id, **dict(args or {})})
    if action == "mesh":
        return await _export_mesh_file({"session_id": session_id, **dict(args or {})})
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
        resolved_event_id = await _ensure_event(session_id, _as_int(args.get("event_id"), 0))
        output_dir = Path(str(args["output_dir"]))
        output_dir.mkdir(parents=True, exist_ok=True)
        stage_payloads = []
        for stage in _stage_candidates():
            shader_resp = await _dispatch_pipeline(
                "get_shader",
                {"session_id": session_id, "stage": stage, "event_id": resolved_event_id},
            )
            shader_payload = json.loads(shader_resp)
            if shader_payload.get("success") and shader_payload.get("shader", {}).get("shader_id"):
                stage_payloads.append(
                    {
                        **dict(shader_payload["shader"]),
                        "resolved_event_id": int(shader_payload.get("resolved_event_id") or resolved_event_id),
                    }
                )
        if not stage_payloads:
            return _err(
                f"No bound shaders available for event {int(resolved_event_id)}",
                code="shader_bundle_empty",
                category="runtime",
                details={"session_id": session_id, "resolved_event_id": int(resolved_event_id)},
            )
        bundle_json = output_dir / "shader_bundle.json"
        bundle_json.write_text(
            json.dumps(
                {
                    "requested_event_id": _as_int(args.get("event_id"), 0),
                    "resolved_event_id": int(resolved_event_id),
                    "shaders": stage_payloads,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return _ok(output_dir=str(output_dir), bundle_path=str(bundle_json), resolved_event_id=int(resolved_event_id))
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
    snapshot = _context_snapshot()

    def _resolve_macro_pixel() -> Tuple[int, int, Any]:
        x_value = args.get("x")
        y_value = args.get("y")
        target_value = args.get("target")
        focus_pixel = snapshot.get("focus", {}).get("pixel")
        if isinstance(focus_pixel, dict):
            if x_value is None:
                x_value = focus_pixel.get("x")
            if y_value is None:
                y_value = focus_pixel.get("y")
            if target_value is None and isinstance(focus_pixel.get("target"), dict):
                target_value = dict(focus_pixel.get("target") or {})
        if x_value is None or y_value is None:
            raise ValueError("Missing required parameter(s): x, y")
        return int(x_value), int(y_value), target_value

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

    if action == "explain_pixel":
        x_value, y_value, target_value = _resolve_macro_pixel()
        history_resp = await _dispatch_debug("pixel_history", {"session_id": session_id, "x": x_value, "y": y_value, "target": target_value})
        payload = json.loads(history_resp)
        if not payload.get("success"):
            return history_resp
        history = payload.get("history", [])
        explanation = f"Pixel({x_value},{y_value}) has {len(history)} recorded modifications in pixel history."
        return _ok(explanation=explanation, history=history[:20])

    if action == "resource_dependency_graph":
        event_range = _as_dict(args.get("event_range"), default={})
        start_evt = _as_int(event_range.get("start_event_id"), 0)
        end_evt = _as_int(event_range.get("end_event_id"), 0)
        max_nodes = max(1, _as_int(args.get("max_nodes"), 128))
        max_edges = max(0, _as_int(args.get("max_edges"), 256))

        if start_evt > 0 and end_evt >= start_evt:
            graph = {"nodes": [], "edges": []}
            previous_node_id = None
            for evt in range(start_evt, end_evt + 1):
                if len(graph["nodes"]) >= max_nodes:
                    break
                detail_resp = await _dispatch_event(
                    "get_action_details",
                    {"session_id": session_id, "event_id": evt},
                )
                detail_payload = json.loads(detail_resp)
                if not detail_payload.get("success"):
                    if evt == start_evt and not graph["nodes"]:
                        return detail_resp
                    continue
                action_payload = _as_dict(detail_payload.get("action"), default={})
                node_id = f"evt_{evt}"
                graph["nodes"].append(
                    {
                        "id": node_id,
                        "label": str(action_payload.get("name") or f"Event {evt}"),
                        "event_id": int(evt),
                    }
                )
                if previous_node_id is not None and len(graph["edges"]) < max_edges:
                    graph["edges"].append({"from": previous_node_id, "to": node_id})
                previous_node_id = node_id
            return _ok(graph=graph)

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
                if event_id is not None and cid is not None and len(graph["edges"]) < max_edges:
                    graph["edges"].append({"from": f"evt_{event_id}", "to": f"evt_{cid}"})
                if len(graph["nodes"]) < max_nodes:
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

    if action == "quick_triage_missing_draw":
        summary_resp = await _dispatch_macro("summarize_frame", {"session_id": session_id})
        diag_resp = await _dispatch_diag("scan_common_issues", {"session_id": session_id, "include_suggestions": True})
        return _ok(
            summary=json.loads(summary_resp),
            diagnostics=json.loads(diag_resp),
            context_snapshot=snapshot,
            recent_artifacts=list(snapshot.get("last_artifacts") or []),
        )

    if action == "build_bug_report_pack":
        _require(args, "output_path")
        output_path = Path(str(args["output_path"]))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        summary_resp = await _dispatch_macro("summarize_frame", {"session_id": session_id})
        summary_payload = json.loads(summary_resp)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("report/summary.json", json.dumps(summary_payload, ensure_ascii=False, indent=2))
            zf.writestr("report/context_snapshot.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
        return _ok(saved_path=str(output_path), context_snapshot=snapshot, recent_artifacts=list(snapshot.get("last_artifacts") or []))

    if action == "shader_hotfix_validate":
        _require(args, "replacement")
        replacement_args = _as_dict(args["replacement"])
        validation_args = _as_dict(args.get("validation"))
        output_root = Path(str(args.get("output_dir") or artifacts_dir()))
        output_root.mkdir(parents=True, exist_ok=True)
        before_path = output_root / "before_hotfix.png"
        after_path = output_root / "after_hotfix.png"
        target_texture_id = str(
            validation_args.get("target_texture_id")
            or validation_args.get("texture_id")
            or ""
        ).strip()
        screenshot_args: Dict[str, Any] = {
            "session_id": session_id,
            "event_id": replacement_args.get("event_id"),
            "output_path": str(before_path),
        }
        if target_texture_id:
            screenshot_args["target"] = {"texture_id": target_texture_id}
        before_pixel_payload: Optional[Dict[str, Any]] = None
        pixel_args: Optional[Dict[str, Any]] = None
        pixel_x = validation_args.get("x")
        pixel_y = validation_args.get("y")
        if target_texture_id and pixel_x is not None and pixel_y is not None:
            pixel_args = {
                "session_id": session_id,
                "texture_id": target_texture_id,
                "x": _as_int(pixel_x),
                "y": _as_int(pixel_y),
                "event_id": replacement_args.get("event_id"),
                "mip": 0,
                "slice": 0,
                "sample": 0,
                "as_type": "float",
            }
        screenshot_before = await _dispatch_export(
            "screenshot",
            screenshot_args,
        )
        if pixel_args is not None:
            before_pixel_payload = json.loads(
                await _dispatch_texture("get_pixel_value", pixel_args)
            )
        repl_resp = await _dispatch_shader("edit_and_replace", {"session_id": session_id, **replacement_args})
        repl_payload = json.loads(repl_resp)
        if not repl_payload.get("success"):
            return repl_resp
        screenshot_args["output_path"] = str(after_path)
        screenshot_after = await _dispatch_export(
            "screenshot",
            screenshot_args,
        )
        before_payload = json.loads(screenshot_before)
        after_payload = json.loads(screenshot_after)

        validation_result: Dict[str, Any] = {
            "target_texture_id": target_texture_id,
        }

        if pixel_args is not None:
            validation_result["before_pixel"] = before_pixel_payload
            validation_result["after_pixel"] = json.loads(
                await _dispatch_texture("get_pixel_value", pixel_args)
            )

        metric = str(validation_args.get("metric") or "").strip().lower()
        if metric:
            validation_result["image_diff"] = json.loads(
                await _dispatch_util(
                    "diff_images",
                    {
                        "image_a_path": str(before_path),
                        "image_b_path": str(after_path),
                        "metrics": [metric],
                    },
                )
            )

        return _ok(
            replacement=repl_payload,
            before=before_payload,
            after=after_payload,
            validation=validation_result,
            output_dir=str(output_root),
            artifacts={"before": str(before_path), "after": str(after_path)},
        )

    return _err(f"Unsupported macro action: {action}")


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
        projection = _projection_request(args, "rd.util.list_artifacts")
        if projection:
            return _ok(
                artifacts=artifacts,
                projections=_artifact_rows_projection(
                    artifacts,
                    include_tsv_text=bool(projection.get("include_tsv_text", False)),
                ),
            )
        return _ok(artifacts=artifacts)

    if action == "cleanup_artifacts":
        result = _artifact_store.cleanup_artifacts(
            older_than_ms=args.get("older_than_ms"),
            prefix=str(args.get("prefix", "")),
            max_total_bytes=args.get("max_total_bytes"),
        )
        return _ok(**result)

    return _err(f"Unsupported util action: {action}")


def _vfs_normalize_path(raw: Any) -> str:
    text = str(raw or "/").strip().replace("\\", "/")
    if not text:
        return "/"
    if not text.startswith("/"):
        text = "/" + text
    parts = [part for part in text.split("/") if part]
    return "/" + "/".join(parts) if parts else "/"


def _vfs_parts(path: str) -> List[str]:
    normalized = _vfs_normalize_path(path)
    if normalized == "/":
        return []
    return [part for part in normalized.split("/") if part]


def _vfs_entry(
    name: str,
    path: str,
    *,
    kind: str = "directory",
    title: str = "",
    summary: str = "",
    requires_session: bool = False,
    canonical_tools: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    return {
        "name": str(name),
        "path": _vfs_normalize_path(path),
        "kind": kind,
        "title": str(title or ""),
        "summary": str(summary or title or ""),
        "exists": True,
        "requires_session": bool(requires_session),
        "canonical_tools": list(canonical_tools or []),
    }


def _vfs_node(
    path: str,
    *,
    kind: str,
    title: str,
    summary: str = "",
    requires_session: bool = False,
    canonical_tools: Optional[Sequence[str]] = None,
    data: Any = None,
    entries: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    normalized = _vfs_normalize_path(path)
    parts = _vfs_parts(normalized)
    name = "/" if not parts else parts[-1]
    payload: Dict[str, Any] = {
        "path": normalized,
        "name": name,
        "kind": kind,
        "title": title,
        "summary": str(summary or title or ""),
        "exists": True,
        "requires_session": bool(requires_session),
        "canonical_tools": list(canonical_tools or []),
    }
    if data is not None:
        payload["data"] = data
    if entries is not None:
        payload["entries"] = list(entries)
    return payload


async def _vfs_call(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    from rdx import server as server_entry

    payload = await server_entry.dispatch_operation(
        tool_name,
        dict(args or {}),
        transport="vfs",
        remote=tool_name.startswith("rd.remote."),
        context_id=_runtime_context_id(),
    )
    if not payload.get("ok"):
        error = payload.get("error", {}) if isinstance(payload.get("error"), dict) else {}
        raise ValueError(str(error.get("message") or f"{tool_name} failed"))
    return dict(payload.get("data") or {})


def _vfs_default_session_id() -> str:
    snapshot = _context_snapshot(_runtime_context_id())
    runtime_payload = snapshot.get("runtime", {})
    return str(runtime_payload.get("session_id") or "").strip()


def _vfs_require_session_id(path: str, args: Dict[str, Any]) -> str:
    session_id = str(args.get("session_id") or "").strip()
    if session_id:
        return session_id
    session_id = _vfs_default_session_id()
    if session_id:
        return session_id
    raise ValueError(f"{_vfs_normalize_path(path)} requires session_id or an active context session")


def _vfs_parse_index(segment: str, *, path: str) -> int:
    try:
        return int(segment)
    except ValueError as exc:
        raise ValueError(f"{_vfs_normalize_path(path)} expects a numeric index segment") from exc


async def _vfs_root_node() -> Dict[str, Any]:
    entries = [
        _vfs_entry("context", "/context", kind="object", title="Current context snapshot", canonical_tools=["rd.session.get_context"]),
        _vfs_entry("artifacts", "/artifacts", title="Recent artifacts and exports", canonical_tools=["rd.util.list_artifacts"]),
        _vfs_entry("draws", "/draws", title="Action tree and draw hierarchy", requires_session=True, canonical_tools=["rd.event.get_action_tree", "rd.event.get_action_details"]),
        _vfs_entry("passes", "/passes", title="Inferred render passes", requires_session=True, canonical_tools=["rd.event.list_passes", "rd.event.search_actions"]),
        _vfs_entry("resources", "/resources", title="All textures and buffers", requires_session=True, canonical_tools=["rd.resource.list_all", "rd.resource.get_details"]),
        _vfs_entry("textures", "/textures", title="Texture inventory", requires_session=True, canonical_tools=["rd.resource.list_textures", "rd.texture.get_data"]),
        _vfs_entry("buffers", "/buffers", title="Buffer inventory", requires_session=True, canonical_tools=["rd.resource.list_buffers", "rd.buffer.get_data"]),
        _vfs_entry("pipeline", "/pipeline", title="Current pipeline snapshot", requires_session=True, canonical_tools=["rd.pipeline.get_state", "rd.pipeline.get_state_summary"]),
        _vfs_entry("shaders", "/shaders", title="Current bound shaders", requires_session=True, canonical_tools=["rd.pipeline.get_state", "rd.pipeline.get_shader"]),
        _vfs_entry("debug", "/debug", title="Debug focus and entry hints", canonical_tools=["rd.session.get_context", "rd.debug.pixel_history", "rd.macro.explain_pixel"]),
    ]
    return _vfs_node("/", kind="directory", title="RDX VFS root", entries=entries)


async def _vfs_draws_node(parts: List[str], path: str, args: Dict[str, Any]) -> Dict[str, Any]:
    session_id = _vfs_require_session_id(path, args)
    if len(parts) == 1:
        payload = await _vfs_call("rd.event.get_action_tree", {"session_id": session_id, "max_depth": args.get("max_depth") or 2})
        root = payload.get("root", {})
        children = list(root.get("children") or []) if isinstance(root, dict) else []
        entries = [
            _vfs_entry(
                str(child.get("event_id", "")),
                f"/draws/{child.get('event_id', '')}",
                kind="object",
                title=str(child.get("name", "")),
                requires_session=True,
                canonical_tools=["rd.event.get_action_details"],
            )
            for child in children
            if child.get("event_id") is not None
        ]
        return _vfs_node("/draws", kind="directory", title="Action tree roots", requires_session=True, canonical_tools=["rd.event.get_action_tree"], data=root, entries=entries)

    event_id = _as_int(parts[1])
    if len(parts) == 2:
        payload = await _vfs_call("rd.event.get_action_details", {"session_id": session_id, "event_id": event_id})
        action = payload.get("action", {})
        entries = [
            _vfs_entry("children", f"/draws/{event_id}/children", title="Immediate child actions", requires_session=True, canonical_tools=["rd.event.get_drawcall_children", "rd.event.get_action_details"]),
            _vfs_entry("pipeline", f"/draws/{event_id}/pipeline", kind="object", title="Pipeline snapshot at this event", requires_session=True, canonical_tools=["rd.pipeline.get_state"]),
            _vfs_entry("shaders", f"/draws/{event_id}/shaders", title="Bound shaders at this event", requires_session=True, canonical_tools=["rd.pipeline.get_state", "rd.pipeline.get_shader"]),
        ]
        return _vfs_node(path, kind="object", title=str(action.get("name", f"draw {event_id}")), requires_session=True, canonical_tools=["rd.event.get_action_details"], data=action, entries=entries)

    tail = parts[2]
    if tail == "children":
        payload = await _vfs_call("rd.event.get_action_details", {"session_id": session_id, "event_id": event_id})
        action = payload.get("action", {})
        children = list(action.get("children") or []) if isinstance(action, dict) else []
        entries = [
            _vfs_entry(
                str(child.get("event_id", "")),
                f"/draws/{child.get('event_id', '')}",
                kind="object",
                title=str(child.get("name", "")),
                requires_session=True,
                canonical_tools=["rd.event.get_action_details"],
            )
            for child in children
            if child.get("event_id") is not None
        ]
        return _vfs_node(path, kind="directory", title=f"Children of draw {event_id}", requires_session=True, canonical_tools=["rd.event.get_action_details"], data={"parent_event_id": event_id, "children": children}, entries=entries)
    if tail == "pipeline":
        if len(parts) == 3:
            payload = await _vfs_call("rd.pipeline.get_state", {"session_id": session_id, "event_id": event_id})
            state = payload.get("pipeline_state", {})
            entries = [
                _vfs_entry("summary", f"/draws/{event_id}/pipeline/summary", kind="object", title="Pipeline summary", requires_session=True, canonical_tools=["rd.pipeline.get_state_summary"]),
                _vfs_entry("shaders", f"/draws/{event_id}/pipeline/shaders", title="Bound shaders", requires_session=True, canonical_tools=["rd.pipeline.get_state", "rd.pipeline.get_shader"]),
            ]
            return _vfs_node(path, kind="object", title=f"Pipeline at draw {event_id}", requires_session=True, canonical_tools=["rd.pipeline.get_state"], data=state, entries=entries)
        if len(parts) >= 4 and parts[3] == "summary":
            payload = await _vfs_call("rd.pipeline.get_state_summary", {"session_id": session_id, "event_id": event_id})
            return _vfs_node(path, kind="object", title=f"Pipeline summary at draw {event_id}", requires_session=True, canonical_tools=["rd.pipeline.get_state_summary"], data=payload.get("summary", {}))
        if len(parts) >= 4 and parts[3] == "shaders":
            payload = await _vfs_call("rd.pipeline.get_state", {"session_id": session_id, "event_id": event_id})
            shaders = list(payload.get("pipeline_state", {}).get("shaders", []) or [])
            if len(parts) == 4:
                entries = [
                    _vfs_entry(
                        str(shader.get("stage", "")).lower(),
                        f"/draws/{event_id}/pipeline/shaders/{str(shader.get('stage', '')).lower()}",
                        kind="object",
                        title=str(shader.get("entry", "") or shader.get("stage", "")),
                        requires_session=True,
                        canonical_tools=["rd.pipeline.get_shader"],
                    )
                    for shader in shaders
                    if shader.get("stage")
                ]
                return _vfs_node(path, kind="directory", title=f"Pipeline shaders at draw {event_id}", requires_session=True, canonical_tools=["rd.pipeline.get_state", "rd.pipeline.get_shader"], data={"shaders": shaders}, entries=entries)
            stage = str(parts[4]).lower()
            payload = await _vfs_call("rd.pipeline.get_shader", {"session_id": session_id, "event_id": event_id, "stage": stage})
            return _vfs_node(path, kind="object", title=f"{stage.upper()} shader at draw {event_id}", requires_session=True, canonical_tools=["rd.pipeline.get_shader"], data=payload.get("shader", {}))
    if tail == "shaders":
        payload = await _vfs_call("rd.pipeline.get_state", {"session_id": session_id, "event_id": event_id})
        shaders = list(payload.get("pipeline_state", {}).get("shaders", []) or [])
        if len(parts) == 3:
            entries = [
                _vfs_entry(
                    str(shader.get("stage", "")).lower(),
                    f"/draws/{event_id}/shaders/{str(shader.get('stage', '')).lower()}",
                    kind="object",
                    title=str(shader.get("entry", "") or shader.get("stage", "")),
                    requires_session=True,
                    canonical_tools=["rd.pipeline.get_shader"],
                )
                for shader in shaders
                if shader.get("stage")
            ]
            return _vfs_node(path, kind="directory", title=f"Bound shaders at draw {event_id}", requires_session=True, canonical_tools=["rd.pipeline.get_state", "rd.pipeline.get_shader"], data={"shaders": shaders}, entries=entries)
        stage = str(parts[3]).lower()
        payload = await _vfs_call("rd.pipeline.get_shader", {"session_id": session_id, "event_id": event_id, "stage": stage})
        return _vfs_node(path, kind="object", title=f"{stage.upper()} shader at draw {event_id}", requires_session=True, canonical_tools=["rd.pipeline.get_shader"], data=payload.get("shader", {}))

    raise ValueError(f"Unsupported VFS path: {_vfs_normalize_path(path)}")


async def _vfs_passes_node(parts: List[str], path: str, args: Dict[str, Any]) -> Dict[str, Any]:
    session_id = _vfs_require_session_id(path, args)
    payload = await _vfs_call("rd.event.list_passes", {"session_id": session_id, "marker_policy": args.get("marker_policy", "both")})
    passes = list(payload.get("passes", []) or [])
    if len(parts) == 1:
        entries = [
            _vfs_entry(str(index), f"/passes/{index}", kind="object", title=str(item.get("name", "")), requires_session=True, canonical_tools=["rd.event.list_passes", "rd.event.search_actions"])
            for index, item in enumerate(passes)
        ]
        return _vfs_node("/passes", kind="directory", title="Pass list", requires_session=True, canonical_tools=["rd.event.list_passes"], data={"passes": passes}, entries=entries)

    index = _vfs_parse_index(parts[1], path=path)
    if index < 0 or index >= len(passes):
        raise ValueError(f"Pass index out of range: {index}")
    selected = passes[index]
    if len(parts) == 2:
        entries = [
            _vfs_entry("draws", f"/passes/{index}/draws", title="Draws inside this pass", requires_session=True, canonical_tools=["rd.event.search_actions"]),
        ]
        return _vfs_node(path, kind="object", title=str(selected.get("name", f"pass {index}")), requires_session=True, canonical_tools=["rd.event.list_passes"], data=selected, entries=entries)
    if parts[2] == "draws":
        query = {
            "event_id_min": int(selected.get("begin_event_id") or 0),
            "event_id_max": int(selected.get("end_event_id") or 0),
        }
        matches_payload = await _vfs_call("rd.event.search_actions", {"session_id": session_id, "query": query, "max_results": args.get("max_results") or 500})
        matches = [
            item
            for item in list(matches_payload.get("matches", []) or [])
            if isinstance(item, dict) and bool((item.get("flags") or {}).get("is_draw"))
        ]
        entries = [
            _vfs_entry(str(item.get("event_id", "")), f"/draws/{item.get('event_id', '')}", kind="object", title=str(item.get("name", "")), requires_session=True, canonical_tools=["rd.event.get_action_details"])
            for item in matches
            if item.get("event_id") is not None
        ]
        return _vfs_node(path, kind="directory", title=f"Draws in pass {index}", requires_session=True, canonical_tools=["rd.event.search_actions"], data={"pass": selected, "draws": matches}, entries=entries)

    raise ValueError(f"Unsupported VFS path: {_vfs_normalize_path(path)}")


async def _vfs_resource_like_node(parts: List[str], path: str, args: Dict[str, Any], *, root_name: str) -> Dict[str, Any]:
    session_id = _vfs_require_session_id(path, args)
    list_tool = {
        "resources": "rd.resource.list_all",
        "textures": "rd.resource.list_textures",
        "buffers": "rd.resource.list_buffers",
    }[root_name]
    key_name = {
        "resources": "resources",
        "textures": "textures",
        "buffers": "buffers",
    }[root_name]
    payload = await _vfs_call(list_tool, {"session_id": session_id})
    items = list(payload.get(key_name, []) or [])
    if len(parts) == 1:
        entries = []
        for item in items:
            item_id = str(item.get("resource_id") or item.get("texture_id") or item.get("buffer_id") or "").strip()
            if not item_id:
                continue
            title = str(item.get("name", "") or item.get("resource_name", "") or item_id)
            entries.append(_vfs_entry(item_id, f"/{root_name}/{item_id}", kind="object", title=title, requires_session=True, canonical_tools=["rd.resource.get_details"]))
        return _vfs_node(path, kind="directory", title=f"{root_name} list", requires_session=True, canonical_tools=[list_tool], data={key_name: items}, entries=entries)

    item_id = str(parts[1]).strip()
    selected = next((item for item in items if str(item.get("resource_id") or item.get("texture_id") or item.get("buffer_id") or "").strip() == item_id), None)
    if selected is None:
        raise ValueError(f"{root_name[:-1]} not found: {item_id}")

    if len(parts) == 2:
        entries = []
        if root_name in {"resources", "textures"}:
            entries.append(_vfs_entry("data", f"/{root_name}/{item_id}/data", kind="object", title="Texture readback container metadata", requires_session=True, canonical_tools=["rd.texture.get_data"]))
        if root_name in {"resources", "buffers"}:
            entries.append(_vfs_entry("usage", f"/{root_name}/{item_id}/usage", kind="object", title="Resource usage", requires_session=True, canonical_tools=["rd.resource.get_usage"]))
            entries.append(_vfs_entry("history", f"/{root_name}/{item_id}/history", kind="object", title="Resource history", requires_session=True, canonical_tools=["rd.resource.get_history"]))
        if root_name in {"buffers"}:
            entries.insert(0, _vfs_entry("data", f"/{root_name}/{item_id}/data", kind="object", title="Buffer data readback metadata", requires_session=True, canonical_tools=["rd.buffer.get_data"]))
        return _vfs_node(path, kind="object", title=str(selected.get("name", item_id)), requires_session=True, canonical_tools=["rd.resource.get_details"], data=selected, entries=entries)

    tail = parts[2]
    if tail == "usage":
        payload = await _vfs_call("rd.resource.get_usage", {"session_id": session_id, "resource_id": item_id})
        return _vfs_node(path, kind="object", title=f"Usage for {item_id}", requires_session=True, canonical_tools=["rd.resource.get_usage"], data={"resource_id": item_id, "usage": payload.get("usage", [])})
    if tail == "history":
        payload = await _vfs_call("rd.resource.get_history", {"session_id": session_id, "resource_id": item_id})
        return _vfs_node(path, kind="object", title=f"History for {item_id}", requires_session=True, canonical_tools=["rd.resource.get_history"], data={"resource_id": item_id, "history": payload.get("history", [])})
    if tail == "data" and root_name in {"resources", "textures"}:
        payload = await _vfs_call("rd.texture.get_data", {"session_id": session_id, "texture_id": item_id, "subresource": {"mip": 0, "slice": 0, "sample": 0}})
        return _vfs_node(path, kind="object", title=f"Texture readback container for {item_id}", requires_session=True, canonical_tools=["rd.texture.get_data"], data=payload)
    if tail == "data" and root_name == "buffers":
        payload = await _vfs_call("rd.buffer.get_data", {"session_id": session_id, "buffer_id": item_id, "offset": 0, "size": 0})
        return _vfs_node(path, kind="object", title=f"Buffer data for {item_id}", requires_session=True, canonical_tools=["rd.buffer.get_data"], data=payload)

    raise ValueError(f"Unsupported VFS path: {_vfs_normalize_path(path)}")


async def _vfs_pipeline_like_node(parts: List[str], path: str, args: Dict[str, Any], *, event_id: Optional[int] = None) -> Dict[str, Any]:
    session_id = _vfs_require_session_id(path, args)
    call_args: Dict[str, Any] = {"session_id": session_id}
    if event_id is not None:
        call_args["event_id"] = int(event_id)
    state_payload = await _vfs_call("rd.pipeline.get_state", call_args)
    state = state_payload.get("pipeline_state", {})
    title_suffix = f" at event {event_id}" if event_id is not None else ""
    base_path = _vfs_normalize_path(path)

    if len(parts) == 1 or (event_id is not None and len(parts) == 3):
        entries = [
            _vfs_entry("summary", f"{base_path}/summary", kind="object", title="Pipeline summary", requires_session=True, canonical_tools=["rd.pipeline.get_state_summary"]),
            _vfs_entry("shaders", f"{base_path}/shaders", title="Bound shaders", requires_session=True, canonical_tools=["rd.pipeline.get_state", "rd.pipeline.get_shader"]),
        ]
        return _vfs_node(base_path, kind="object", title=f"Pipeline{title_suffix}", requires_session=True, canonical_tools=["rd.pipeline.get_state"], data=state, entries=entries)

    tail_index = 1 if event_id is None else 3
    tail = parts[tail_index]
    if tail == "summary":
        summary_payload = await _vfs_call("rd.pipeline.get_state_summary", call_args)
        return _vfs_node(path, kind="object", title=f"Pipeline summary{title_suffix}", requires_session=True, canonical_tools=["rd.pipeline.get_state_summary"], data=summary_payload.get("summary", {}))
    if tail == "shaders":
        shaders = list(state.get("shaders", []) or []) if isinstance(state, dict) else []
        if len(parts) == tail_index + 1:
            entries = [
                _vfs_entry(
                    str(shader.get("stage", "")).lower(),
                    f"{base_path}/shaders/{str(shader.get('stage', '')).lower()}",
                    kind="object",
                    title=str(shader.get("entry", "") or shader.get("stage", "")),
                    requires_session=True,
                    canonical_tools=["rd.pipeline.get_shader"],
                )
                for shader in shaders
                if shader.get("stage")
            ]
            return _vfs_node(path, kind="directory", title=f"Pipeline shaders{title_suffix}", requires_session=True, canonical_tools=["rd.pipeline.get_state", "rd.pipeline.get_shader"], data={"shaders": shaders}, entries=entries)
        stage = str(parts[tail_index + 1]).lower()
        shader_payload = await _vfs_call("rd.pipeline.get_shader", {**call_args, "stage": stage})
        return _vfs_node(path, kind="object", title=f"{stage.upper()} shader{title_suffix}", requires_session=True, canonical_tools=["rd.pipeline.get_shader"], data=shader_payload.get("shader", {}))

    raise ValueError(f"Unsupported VFS path: {_vfs_normalize_path(path)}")


async def _vfs_context_node(path: str) -> Dict[str, Any]:
    payload = await _vfs_call("rd.session.get_context", {})
    entries = [
        _vfs_entry("runtime", "/context", kind="object", title="Runtime snapshot", canonical_tools=["rd.session.get_context"]),
    ]
    return _vfs_node(path, kind="object", title="Current context snapshot", canonical_tools=["rd.session.get_context"], data=payload, entries=entries)


async def _vfs_artifacts_node(parts: List[str], path: str, args: Dict[str, Any]) -> Dict[str, Any]:
    payload = await _vfs_call("rd.util.list_artifacts", {"session_id": args.get("session_id"), "prefix": args.get("prefix", "")})
    artifacts = list(payload.get("artifacts", []) or [])
    if len(parts) == 1:
        entries = [
            _vfs_entry(str(index), f"/artifacts/{index}", kind="object", title=str(item.get("path", "")), canonical_tools=["rd.util.list_artifacts"])
            for index, item in enumerate(artifacts)
        ]
        return _vfs_node(path, kind="directory", title="Artifacts", canonical_tools=["rd.util.list_artifacts"], data={"artifacts": artifacts}, entries=entries)
    index = _vfs_parse_index(parts[1], path=path)
    if index < 0 or index >= len(artifacts):
        raise ValueError(f"Artifact index out of range: {index}")
    return _vfs_node(path, kind="object", title=f"Artifact {index}", canonical_tools=["rd.util.list_artifacts"], data=artifacts[index])


async def _vfs_shaders_node(parts: List[str], path: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return await _vfs_pipeline_like_node(parts, "/pipeline/shaders" if path == "/shaders" else path, args)


async def _vfs_debug_node(parts: List[str], path: str) -> Dict[str, Any]:
    snapshot = _context_snapshot(_runtime_context_id())
    focus = snapshot.get("focus", {})
    entries = [
        _vfs_entry("focus", "/debug/focus", kind="object", title="Current focus hints", canonical_tools=["rd.session.get_context"]),
    ]
    if len(parts) == 1:
        return _vfs_node(path, kind="directory", title="Debug focus and hints", canonical_tools=["rd.session.get_context", "rd.debug.pixel_history", "rd.macro.explain_pixel"], data={"focus": focus, "recommended_tools": ["rd.debug.pixel_history", "rd.macro.explain_pixel"]}, entries=entries)
    if len(parts) == 2 and parts[1] == "focus":
        return _vfs_node(path, kind="object", title="Current focus", canonical_tools=["rd.session.get_context"], data=focus)
    raise ValueError(f"Unsupported VFS path: {_vfs_normalize_path(path)}")


async def _vfs_resolve_node(path: str, args: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _vfs_normalize_path(path)
    parts = _vfs_parts(normalized)
    if not parts:
        return await _vfs_root_node()
    head = parts[0]
    if head == "context":
        return await _vfs_context_node(normalized)
    if head == "artifacts":
        return await _vfs_artifacts_node(parts, normalized, args)
    if head == "draws":
        return await _vfs_draws_node(parts, normalized, args)
    if head == "passes":
        return await _vfs_passes_node(parts, normalized, args)
    if head == "resources":
        return await _vfs_resource_like_node(parts, normalized, args, root_name="resources")
    if head == "textures":
        return await _vfs_resource_like_node(parts, normalized, args, root_name="textures")
    if head == "buffers":
        return await _vfs_resource_like_node(parts, normalized, args, root_name="buffers")
    if head == "pipeline":
        return await _vfs_pipeline_like_node(parts, normalized, args)
    if head == "shaders":
        session_id = _vfs_require_session_id(normalized, args)
        rewritten = ["pipeline", "shaders", *parts[1:]]
        return await _vfs_pipeline_like_node(rewritten, "/pipeline/shaders" if len(rewritten) == 2 else f"/pipeline/shaders/{'/'.join(rewritten[2:])}", {"session_id": session_id})
    if head == "debug":
        return await _vfs_debug_node(parts, normalized)
    raise ValueError(f"Unsupported VFS path: {normalized}")


async def _vfs_build_tree(path: str, args: Dict[str, Any], depth: int) -> Dict[str, Any]:
    node = await _vfs_resolve_node(path, args)
    if depth <= 0:
        return node
    entries = list(node.get("entries") or []) if isinstance(node, dict) else []
    if not entries:
        return node
    children = []
    for entry in entries:
        child_path = str(entry.get("path") or "").strip()
        if not child_path:
            continue
        children.append(await _vfs_build_tree(child_path, args, depth - 1))
    enriched = dict(node)
    enriched["children"] = children
    return enriched


async def _dispatch_vfs(action: str, args: Dict[str, Any]) -> str:
    path = _vfs_normalize_path(args.get("path"))
    if args.get("projection") is not None and action != "ls":
        return _err(
            f"rd.vfs.{action} does not support tabular projection",
            code="projection_not_supported",
            category="validation",
            details={"tool_name": f"rd.vfs.{action}", "supported_projection": "tabular", "supported_actions": ["ls"]},
        )
    if action == "ls":
        node = await _vfs_resolve_node(path, args)
        entries = list(node.get("entries") or [])
        projection = _projection_request(args, "rd.vfs.ls")
        if projection:
            return _ok(
                path=path,
                node=node,
                entries=entries,
                projections=_vfs_entries_projection(
                    entries,
                    include_tsv_text=bool(projection.get("include_tsv_text", False)),
                ),
            )
        return _ok(path=path, node=node, entries=entries)
    if action == "cat":
        node = await _vfs_resolve_node(path, args)
        return _ok(path=path, node=node)
    if action == "resolve":
        node = await _vfs_resolve_node(path, args)
        return _ok(path=path, node=node)
    if action == "tree":
        depth = max(0, _as_int(args.get("depth"), 2))
        tree = await _vfs_build_tree(path, args, depth)
        return _ok(path=path, tree=tree)
    return _err(f"Unsupported vfs action: {action}")


async def _dispatch_remote(action: str, args: Dict[str, Any]) -> str:
    if action == "connect":
        options = _as_dict(args.get("options"), default={})
        transport = str(options.get("transport") or "renderdoc").strip().lower() or "renderdoc"
        host = str(args.get("host") or "").strip()
        if transport != "adb_android" and not host:
            _require(args, "host")
        if not host:
            host = "127.0.0.1"
        port = _as_int(args.get("port"), 38920)
        timeout_ms = remote_connect_timeout_ms(args)
        if not _runtime.enable_remote:
            return _capability_error(
                "remote_disabled",
                "Remote tools disabled by config",
                capability="remote",
                reason="Remote tools are disabled by config.",
                source="runtime_config",
                optional=True,
            )
        if transport not in {"renderdoc", "adb_android"}:
            return _err(
                f"Unsupported remote transport: {transport}",
                code="remote_transport_unsupported",
                category="runtime",
                details={"transport": transport},
            )

        endpoint_host = host
        endpoint_port = port
        bootstrap_detail: Dict[str, Any] = {}
        bootstrap_result = None

        try:
            if transport == "adb_android":
                _progress("bootstrap_start", "Bootstrapping Android remote endpoint", progress_pct=0.05, details={"transport": transport, "host": host, "port": port})
                if host not in {"", "127.0.0.1", "localhost"}:
                    return _err(
                        "Android adb transport requires host=127.0.0.1 or localhost",
                        code="android_remote_host_invalid",
                        category="runtime",
                        details={"host": host},
                    )
                bootstrap_result = await _offload(
                    bootstrap_android_remote,
                    remote_port=port,
                    options=AndroidBootstrapOptions(
                        device_serial=str(options.get("device_serial") or ""),
                        local_port=_as_int(options.get("local_port"), 0),
                        install_apk=_as_bool(options.get("install_apk"), True),
                        push_config=_as_bool(options.get("push_config"), True),
                    ),
                )
                bootstrap_detail = describe_android_remote(bootstrap_result)
                endpoint_host = str(bootstrap_result.host)
                endpoint_port = int(bootstrap_result.port)
                _progress("bootstrap_done", "Android bootstrap complete", progress_pct=0.2, details={"endpoint_host": endpoint_host, "endpoint_port": endpoint_port})

            url = _remote_url(endpoint_host, endpoint_port)
            _progress("waiting_endpoint", "Waiting for remote endpoint", progress_pct=0.35, details={"endpoint": url, "timeout_ms": timeout_ms})
            await _offload(_wait_for_remote_endpoint, url, timeout_ms)
            _progress("endpoint_ready", "Remote endpoint is reachable", progress_pct=0.55, details={"endpoint": url})
            remote_server = await _offload(_create_remote_server_connection, url)
            _progress("remote_connection_created", "Remote connection created", progress_pct=0.75, details={"endpoint": url})
            ping_status = await _offload(remote_server.Ping)
            if not _status_ok(ping_status):
                raise RuntimeError(f"RemoteServer.Ping({url}) failed: {_status_text(ping_status)}")
            _progress("remote_ping_ok", "Remote ping succeeded", progress_pct=0.9, details={"endpoint": url})
            server_info = await _offload(
                _collect_remote_server_info,
                remote_server,
                host=endpoint_host,
                port=endpoint_port,
                transport=transport,
                bootstrap=bootstrap_detail,
            )
        except AndroidRemoteBootstrapError as exc:
            return _err(exc.message, code=exc.code, category="runtime", details=exc.details)
        except CoreError as exc:
            if bootstrap_result is not None:
                try:
                    await _offload(cleanup_android_remote, bootstrap_result)
                except Exception:
                    pass
            details = dict(exc.details)
            details.setdefault("transport", transport)
            details.setdefault("host", endpoint_host)
            details.setdefault("port", endpoint_port)
            details.setdefault("requested_host", host)
            details.setdefault("requested_port", port)
            return _err(exc.message, code=exc.code, category=exc.category, details=details)
        except Exception as exc:
            if bootstrap_result is not None:
                try:
                    await _offload(cleanup_android_remote, bootstrap_result)
                except Exception:
                    pass
            return _err(
                str(exc),
                code="remote_connect_failed",
                category="runtime",
                details={
                    "transport": transport,
                    "host": endpoint_host,
                    "port": endpoint_port,
                    "requested_host": host,
                    "requested_port": port,
                    "source_layer": "runtime",
                    "operation": "rd.remote.connect",
                    "backend_type": "remote",
                    "capture_context": {
                        "endpoint": _remote_url(endpoint_host, endpoint_port),
                        "remote_id": "",
                    },
                    "classification": "remote_endpoint",
                    "fix_hint": "Repair the remote endpoint or Android bootstrap path before retrying rd.remote.connect.",
                },
            )

        remote_id = _new_id("remote")
        detail = {
            "connected": True,
            "requires_remote_device": transport == "adb_android",
            "transport": transport,
            "endpoint": _remote_url(endpoint_host, endpoint_port),
        }
        if bootstrap_detail:
            detail["bootstrap"] = dict(bootstrap_detail)
        _runtime.remotes[remote_id] = RemoteHandle(
            remote_id=remote_id,
            host=endpoint_host,
            port=endpoint_port,
            connected=True,
            transport=transport,
            remote_server=remote_server,
            server_info=server_info,
            bootstrap=bootstrap_detail,
            bootstrap_result=bootstrap_result,
            requested_host=host,
            requested_port=port,
            device_serial=str(options.get("device_serial") or ""),
            detail=detail,
        )
        _set_context_remote_live(remote_id, detail["endpoint"])
        _progress("context_synced", "Remote handle synchronized to context", progress_pct=1.0, details={"remote_id": remote_id, "endpoint": detail["endpoint"]})
        return _ok(remote_id=remote_id, server_info=server_info, detail=detail)

    if action == "disconnect":
        _require(args, "remote_id")
        remote_id = str(args["remote_id"])
        consumed = _remote_consumed_payload(remote_id)
        if consumed is not None:
            return consumed
        handle = _runtime.remotes.get(remote_id)
        if handle is None:
            return _err(f"Unknown remote_id: {remote_id}", code="remote_not_found", category="runtime")
        active_session_ids = _remote_active_session_ids(handle)
        if active_session_ids:
            return _err(
                f"Remote handle {remote_id} is still leased by active session(s): {', '.join(active_session_ids)}",
                code="remote_handle_in_use",
                category="runtime",
                details={
                    "remote_id": remote_id,
                    "active_session_ids": active_session_ids,
                },
            )
        handle = _runtime.remotes.pop(remote_id, None)
        if handle is None:
            return _err(f"Unknown remote_id: {remote_id}", code="remote_not_found", category="runtime")
        _clear_context_remote_live(remote_id)
        errors = await _offload(_disconnect_remote_handle_sync, handle)
        if errors:
            return _ok(detail={"connected": False, "cleanup_errors": errors})
        return _ok(detail={"connected": False})

    if action == "ping":
        _require(args, "remote_id")
        remote_id = str(args["remote_id"])
        consumed = _remote_consumed_payload(remote_id)
        if consumed is not None:
            return consumed
        handle = _runtime.remotes.get(remote_id)
        if handle is None:
            return _err(f"Unknown remote_id: {remote_id}", code="remote_not_found", category="runtime")
        if not handle.connected or handle.remote_server is None:
            return _err(
                f"Remote handle {remote_id} is not connected",
                code="remote_not_connected",
                category="runtime",
                details={"remote_id": remote_id},
            )
        started = time.perf_counter()
        try:
            status = await _offload(handle.remote_server.Ping)
        except Exception as exc:
            handle.connected = False
            return _err(
                f"RemoteServer.Ping({_remote_url(handle.host, handle.port)}) failed: {exc}",
                code="remote_ping_failed",
                category="runtime",
                details={
                    "remote_id": remote_id,
                    "source_layer": "runtime",
                    "operation": "rd.remote.ping",
                    "backend_type": "remote",
                    "capture_context": {"remote_id": remote_id, "endpoint": _remote_url(handle.host, handle.port)},
                    "classification": "remote_endpoint",
                    "fix_hint": "Reconnect to the remote endpoint before issuing more remote tools.",
                },
            )
        latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        if not _status_ok(status):
            handle.connected = False
            details = build_renderdoc_error_details(
                status,
                operation=f"RemoteServer.Ping({_remote_url(handle.host, handle.port)})",
                source_layer="renderdoc_status",
                backend_type="remote",
                capture_context={"remote_id": remote_id, "endpoint": _remote_url(handle.host, handle.port), "latency_ms": latency_ms},
                classification="remote_endpoint",
                fix_hint="Reconnect to the remote endpoint before issuing more remote tools.",
            )
            return _err(
                f"RemoteServer.Ping({_remote_url(handle.host, handle.port)}) failed: {details['renderdoc_status']['status_text']}",
                code="remote_ping_failed",
                category="runtime",
                details=details,
            )
        handle.detail["connected"] = True
        return _ok(
            latency_ms=latency_ms,
            server_info=handle.server_info,
            detail={
                "connected": True,
                "transport": handle.transport,
                "endpoint": _remote_url(handle.host, handle.port),
                "active_session_ids": _remote_active_session_ids(handle),
            },
        )

    if action in {
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
        consumed = _remote_consumed_payload(remote_id)
        if consumed is not None:
            return consumed
        handle = _runtime.remotes.get(remote_id)
        if handle is None:
            return _err(f"Unknown remote_id: {remote_id}", code="remote_not_found", category="runtime")
        if not handle.connected or handle.remote_server is None:
            return _err(
                f"Remote handle {remote_id} is not connected",
                code="remote_not_connected",
                category="runtime",
                details={"remote_id": remote_id, "endpoint": _remote_url(handle.host, handle.port)},
            )
        if action == "list_targets":
            try:
                targets = await _offload(_remote_list_targets_sync, handle)
            except Exception as exc:
                return _err(
                    f"Failed to enumerate remote targets: {exc}",
                    code="remote_target_enumeration_failed",
                    category="runtime",
                    details={"remote_id": remote_id, "endpoint": _remote_url(handle.host, handle.port)},
                )
            return _ok(targets=targets, detail={"remote_id": remote_id, "target_count": len(targets)})
        if action == "launch_app":
            _require(args, "exe_path")
            try:
                target = await _offload(
                    _launch_remote_app_sync,
                    handle,
                    exe_path=str(args.get("exe_path") or ""),
                    working_dir=str(args.get("working_dir") or ""),
                    cmdline=str(args.get("cmdline") or ""),
                    env=_as_dict(args.get("env"), default={}),
                    capture_options=_as_dict(args.get("capture_options"), default={}),
                )
            except Exception as exc:
                return _err(
                    f"Failed to launch remote app: {exc}",
                    code="remote_launch_failed",
                    category="runtime",
                    details={"remote_id": remote_id, "endpoint": _remote_url(handle.host, handle.port)},
                )
            return _ok(
                target_id=str(target.get("target_id") or ""),
                pid=int(target.get("pid") or 0),
                target=target,
            )
        if action == "set_capture_options":
            options = _as_dict(args.get("options"), default={})
            _, applied_options = await _offload(_capture_options_from_dict, options)
            handle.default_capture_options = {**dict(handle.default_capture_options or {}), **applied_options}
            return _ok(applied=applied_options, default_capture_options=dict(handle.default_capture_options))
        if action == "set_overlay_options":
            return _capability_error(
                "remote_overlay_options_unavailable",
                "Remote overlay RPC is not exposed by the current RenderDoc Python binding",
                capability="remote",
                reason="TargetControl / RemoteServer does not expose overlay option RPCs in this build.",
                source="renderdoc_api",
                optional=True,
                requires_remote_device=True,
            )
        if action in {"trigger_capture", "queue_capture"}:
            try:
                result = await _offload(
                    _trigger_remote_capture_sync,
                    handle,
                    target_id=str(args.get("target_id") or ""),
                    num_frames=_as_int(args.get("num_frames"), 1),
                    capture_delay_ms=_as_int(args.get("capture_delay_ms"), 0),
                    queued=action == "queue_capture",
                )
            except Exception as exc:
                return _err(
                    f"Failed to {action}: {exc}",
                    code=f"remote_{action}_failed",
                    category="runtime",
                    details={"remote_id": remote_id, "endpoint": _remote_url(handle.host, handle.port)},
                )
            if action == "trigger_capture":
                return _ok(captures=result.get("captures", []), target=result.get("target", {}))
            return _ok(queue_status=result.get("queue_status", {}), captures=result.get("captures", []), target=result.get("target", {}))
        if action == "list_captures":
            try:
                target_id = str(args.get("target_id") or "")
                if target_id:
                    captures = await _offload(_poll_captures_for_target_sync, handle, target_id=target_id, deadline_s=1.0, max_messages=12)
                else:
                    targets = await _offload(_remote_list_targets_sync, handle)
                    captures = _capture_records_for_target(handle)
                    for item in targets:
                        tid = str(item.get("target_id") or "")
                        if not tid:
                            continue
                        captures = await _offload(_poll_captures_for_target_sync, handle, target_id=tid, deadline_s=0.5, max_messages=8)
                    captures = _capture_records_for_target(handle)
            except Exception as exc:
                return _err(
                    f"Failed to list remote captures: {exc}",
                    code="remote_list_captures_failed",
                    category="runtime",
                    details={"remote_id": remote_id, "endpoint": _remote_url(handle.host, handle.port)},
                )
            return _ok(captures=captures)
        if action == "copy_capture":
            _require(args, "capture_id", "local_path")
            capture_id = str(args.get("capture_id") or "").strip()
            record = dict(handle.known_captures.get(capture_id) or {})
            target_id = str(record.get("target_id") or "")
            if not target_id:
                targets = await _offload(_remote_list_targets_sync, handle)
                if len(targets) == 1:
                    target_id = str(targets[0].get("target_id") or "")
            ident = _remote_target_ident(target_id)
            if ident <= 0:
                return _err(
                    f"Unable to resolve target for capture_id: {capture_id}",
                    code="remote_capture_target_unknown",
                    category="runtime",
                    details={"remote_id": remote_id, "capture_id": capture_id},
                )
            local_path = Path(str(args.get("local_path") or "")).resolve()
            local_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                control = await _offload(_create_target_control_sync, handle, ident, force_connection=False)
                await _offload(control.CopyCapture, _as_int(capture_id, 0), str(local_path))
                deadline = time.perf_counter() + 30.0
                while time.perf_counter() < deadline:
                    if local_path.is_file():
                        break
                    await _offload(
                        _drain_target_control_messages_sync,
                        handle,
                        control,
                        target_id=str(ident),
                        max_messages=6,
                        deadline_s=0.2,
                    )
                    await asyncio.sleep(0.1)
            except Exception as exc:
                return _err(
                    f"Failed to copy remote capture: {exc}",
                    code="remote_copy_capture_failed",
                    category="runtime",
                    details={"remote_id": remote_id, "capture_id": capture_id, "local_path": str(local_path)},
                )
            return _ok(saved_path=str(local_path), capture=dict(handle.known_captures.get(capture_id) or record))
        if action == "delete_capture":
            _require(args, "capture_id")
            capture_id = str(args.get("capture_id") or "").strip()
            record = dict(handle.known_captures.get(capture_id) or {})
            target_id = str(record.get("target_id") or "")
            if not target_id:
                targets = await _offload(_remote_list_targets_sync, handle)
                if len(targets) == 1:
                    target_id = str(targets[0].get("target_id") or "")
            ident = _remote_target_ident(target_id)
            if ident <= 0:
                return _err(
                    f"Unable to resolve target for capture_id: {capture_id}",
                    code="remote_capture_target_unknown",
                    category="runtime",
                    details={"remote_id": remote_id, "capture_id": capture_id},
                )
            try:
                control = await _offload(_create_target_control_sync, handle, ident, force_connection=False)
                await _offload(control.DeleteCapture, _as_int(capture_id, 0))
            except Exception as exc:
                return _err(
                    f"Failed to delete remote capture: {exc}",
                    code="remote_delete_capture_failed",
                    category="runtime",
                    details={"remote_id": remote_id, "capture_id": capture_id},
                )
            handle.known_captures.pop(capture_id, None)
            return _ok(detail={"deleted_capture_id": capture_id})
    return _err(f"Unsupported remote action: {action}")



