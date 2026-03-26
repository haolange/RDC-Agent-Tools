"""Persistent context/session state helpers for daemon-backed runtime."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import msvcrt

from rdx.context_snapshot import normalize_context_id
from rdx.io_utils import atomic_append_jsonl, atomic_write_json
from rdx.runtime_paths import cli_runtime_dir

_STATE_MUTEXES: dict[str, threading.Lock] = {}
_STATE_MUTEXES_LOCK = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _context_mutex(context: Optional[str]) -> threading.Lock:
    ctx = normalize_context_id(context)
    with _STATE_MUTEXES_LOCK:
        lock = _STATE_MUTEXES.get(ctx)
        if lock is None:
            lock = threading.Lock()
            _STATE_MUTEXES[ctx] = lock
        return lock


def _sanitize_context(context: Optional[str]) -> str:
    ctx = normalize_context_id(context)
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in ctx)


def context_state_path(context: Optional[str] = "default") -> Path:
    state_dir = cli_runtime_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    ctx = normalize_context_id(context)
    if ctx == "default":
        return state_dir / "runtime_state.json"
    return state_dir / f"runtime_state_{_sanitize_context(ctx)}.json"


def context_log_path(context: Optional[str] = "default") -> Path:
    state_dir = cli_runtime_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    ctx = normalize_context_id(context)
    if ctx == "default":
        return state_dir / "runtime_logs.jsonl"
    return state_dir / f"runtime_logs_{_sanitize_context(ctx)}.jsonl"


def _context_state_lock_path(context: Optional[str] = "default") -> Path:
    return context_state_path(context).with_suffix(".lock")


@contextmanager
def _locked_context_state_file(context: Optional[str] = "default"):
    lock_path = _context_state_lock_path(context)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _context_mutex(context):
        with open(lock_path, "a+b") as handle:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _normalize_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _normalize_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _default_limits() -> Dict[str, Any]:
    return {
        "max_contexts": 8,
        "max_sessions_per_context": 4,
        "max_capture_files": 8,
        "max_capture_size_bytes": 4 * 1024 * 1024 * 1024,
        "max_estimated_replay_memory_bytes": 8 * 1024 * 1024 * 1024,
        "replay_memory_multiplier": 3.0,
        "max_recent_operations": 64,
    }


def _default_runtime_owner() -> Dict[str, Any]:
    return {
        "agent_id": "",
        "lease_id": "",
        "status": "unclaimed",
        "claimed_at_ms": 0,
        "released_at_ms": 0,
    }


def _default_active_baton() -> Dict[str, Any]:
    return {
        "baton_id": "",
        "artifact_path": "",
        "task_goal": "",
        "status": "idle",
        "exported_at_ms": 0,
    }


def _default_rehydrate_status() -> Dict[str, Any]:
    return {
        "status": "idle",
        "baton_id": "",
        "last_attempt_ms": 0,
        "last_success_ms": 0,
        "last_error": "",
    }


def default_context_state(context: Optional[str] = "default", *, limits: Any = None) -> Dict[str, Any]:
    payload_limits = dict(_default_limits())
    if isinstance(limits, dict):
        for key, value in limits.items():
            payload_limits[str(key)] = value
    ctx = normalize_context_id(context)
    now_ms = _now_ms()
    return {
        "schema_version": 1,
        "context_id": ctx,
        "current_capture_file_id": "",
        "current_session_id": "",
        "entry_mode": "cli",
        "backend": "local",
        "runtime_parallelism_ceiling": "multi_context_multi_owner",
        "captures": {},
        "sessions": {},
        "runtime_owner": _default_runtime_owner(),
        "owner_lease": _default_runtime_owner(),
        "active_baton": _default_active_baton(),
        "rehydrate_status": _default_rehydrate_status(),
        "recovery": {
            "status": "idle",
            "last_scan_ms": 0,
            "last_attempt_ms": 0,
            "last_success_ms": 0,
            "attempt_count": 0,
            "recovered_session_ids": [],
            "degraded_session_ids": [],
            "last_error": "",
        },
        "limits": payload_limits,
        "recent_operations": [],
        "metrics": {
            "operation_count": 0,
            "operation_error_count": 0,
            "recovery_attempt_count": 0,
            "recovery_success_count": 0,
            "recovery_failure_count": 0,
            "rejection_count": 0,
            "active_session_count": 0,
            "active_capture_count": 0,
            "active_operation_count": 0,
            "recent_operation_duration_ms": [],
            "last_operation_ms": 0,
            "last_recovery_ms": 0,
        },
        "updated_at_ms": now_ms,
        "created_at_ms": now_ms,
    }


def _normalize_runtime_owner(value: Any) -> Dict[str, Any]:
    item = dict(value or {}) if isinstance(value, dict) else {}
    status = str(item.get("status") or "unclaimed").strip() or "unclaimed"
    return {
        "agent_id": str(item.get("agent_id") or "").strip(),
        "lease_id": str(item.get("lease_id") or "").strip(),
        "status": status,
        "claimed_at_ms": _normalize_int(item.get("claimed_at_ms")),
        "released_at_ms": _normalize_int(item.get("released_at_ms")),
    }


def _normalize_active_baton(value: Any) -> Dict[str, Any]:
    item = dict(value or {}) if isinstance(value, dict) else {}
    return {
        "baton_id": str(item.get("baton_id") or "").strip(),
        "artifact_path": str(item.get("artifact_path") or "").strip(),
        "task_goal": str(item.get("task_goal") or "").strip(),
        "status": str(item.get("status") or "idle").strip() or "idle",
        "exported_at_ms": _normalize_int(item.get("exported_at_ms")),
    }


def _normalize_rehydrate_status(value: Any) -> Dict[str, Any]:
    item = dict(value or {}) if isinstance(value, dict) else {}
    return {
        "status": str(item.get("status") or "idle").strip() or "idle",
        "baton_id": str(item.get("baton_id") or "").strip(),
        "last_attempt_ms": _normalize_int(item.get("last_attempt_ms")),
        "last_success_ms": _normalize_int(item.get("last_success_ms")),
        "last_error": str(item.get("last_error") or "").strip(),
    }


def _normalize_capture_record(capture_file_id: str, value: Any) -> Dict[str, Any]:
    item = dict(value or {}) if isinstance(value, dict) else {}
    return {
        "capture_file_id": str(item.get("capture_file_id") or capture_file_id or "").strip(),
        "file_path": str(item.get("file_path") or "").strip(),
        "read_only": bool(item.get("read_only", True)),
        "driver": str(item.get("driver") or "").strip(),
        "file_size_bytes": _normalize_int(item.get("file_size_bytes")),
        "file_mtime_ms": _normalize_int(item.get("file_mtime_ms")),
        "file_fingerprint": str(item.get("file_fingerprint") or "").strip(),
        "recovery_status": str(item.get("recovery_status") or "ready").strip() or "ready",
        "last_error": str(item.get("last_error") or "").strip(),
        "updated_at_ms": _normalize_int(item.get("updated_at_ms"), _now_ms()),
    }


def _normalize_remote_session_record(value: Any) -> Dict[str, Any]:
    item = dict(value or {}) if isinstance(value, dict) else {}
    bootstrap = item.get("bootstrap") if isinstance(item.get("bootstrap"), dict) else {}
    requested = item.get("requested") if isinstance(item.get("requested"), dict) else {}
    options = item.get("options") if isinstance(item.get("options"), dict) else {}
    return {
        "transport": str(item.get("transport") or "renderdoc").strip() or "renderdoc",
        "host": str(item.get("host") or "").strip(),
        "port": _normalize_int(item.get("port")),
        "endpoint": str(item.get("endpoint") or "").strip(),
        "origin_remote_id": str(item.get("origin_remote_id") or "").strip(),
        "ownership_state": str(item.get("ownership_state") or "session_owned").strip() or "session_owned",
        "device_serial": str(item.get("device_serial") or bootstrap.get("device_serial") or "").strip(),
        "requested": {
            "host": str(requested.get("host") or "").strip(),
            "port": _normalize_int(requested.get("port")),
        },
        "options": {
            "install_apk": bool(options.get("install_apk", True)),
            "push_config": bool(options.get("push_config", True)),
            "local_port": _normalize_int(options.get("local_port")),
            "remote_port": _normalize_int(
                options.get("remote_port") or bootstrap.get("remote_port")
            ),
        },
        "bootstrap": {
            "package_name": str(bootstrap.get("package_name") or "").strip(),
            "activity_name": str(bootstrap.get("activity_name") or "").strip(),
            "abi": str(bootstrap.get("abi") or "").strip(),
            "remote_port": _normalize_int(bootstrap.get("remote_port")),
            "config_remote_path": str(bootstrap.get("config_remote_path") or "").strip(),
        },
    }


def _normalize_session_record(session_id: str, value: Any) -> Dict[str, Any]:
    item = dict(value or {}) if isinstance(value, dict) else {}
    recovery = item.get("recovery") if isinstance(item.get("recovery"), dict) else {}
    remote = item.get("remote") if isinstance(item.get("remote"), dict) else {}
    return {
        "session_id": str(item.get("session_id") or session_id or "").strip(),
        "capture_file_id": str(item.get("capture_file_id") or "").strip(),
        "rdc_path": str(item.get("rdc_path") or "").strip(),
        "file_fingerprint": str(item.get("file_fingerprint") or "").strip(),
        "file_size_bytes": _normalize_int(item.get("file_size_bytes")),
        "frame_index": _normalize_int(item.get("frame_index")),
        "active_event_id": _normalize_int(item.get("active_event_id")),
        "backend_type": str(item.get("backend_type") or "none").strip() or "none",
        "state": str(item.get("state") or "active").strip() or "active",
        "is_live": bool(item.get("is_live", True)),
        "last_error": str(item.get("last_error") or "").strip(),
        "updated_at_ms": _normalize_int(item.get("updated_at_ms"), _now_ms()),
        "remote": _normalize_remote_session_record(remote) if remote else {},
        "recovery": {
            "status": str(recovery.get("status") or "idle").strip() or "idle",
            "last_attempt_ms": _normalize_int(recovery.get("last_attempt_ms")),
            "last_success_ms": _normalize_int(recovery.get("last_success_ms")),
            "attempt_count": _normalize_int(recovery.get("attempt_count")),
            "last_error": str(recovery.get("last_error") or item.get("last_error") or "").strip(),
        },
    }


def _trim_recent_operations(entries: Iterable[Dict[str, Any]], *, limit: int) -> list[Dict[str, Any]]:
    normalized: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in sorted(
        (
            entry
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("trace_id") or "").strip()
        ),
        key=lambda entry: int(entry.get("updated_at_ms") or entry.get("started_at_ms") or 0),
        reverse=True,
    ):
        trace_id = str(item.get("trace_id") or "").strip()
        if trace_id in seen:
            continue
        seen.add(trace_id)
        normalized.append(
            {
                "trace_id": trace_id,
                "operation": str(item.get("operation") or "").strip(),
                "transport": str(item.get("transport") or "").strip(),
                "status": str(item.get("status") or "unknown").strip() or "unknown",
                "args_summary": dict(item.get("args_summary") or {}) if isinstance(item.get("args_summary"), dict) else {},
                "stages": list(item.get("stages") or []) if isinstance(item.get("stages"), list) else [],
                "result_ok": item.get("result_ok"),
                "error_code": str(item.get("error_code") or "").strip(),
                "error_message": str(item.get("error_message") or "").strip(),
                "duration_ms": _normalize_int(item.get("duration_ms")),
                "started_at_ms": _normalize_int(item.get("started_at_ms")),
                "updated_at_ms": _normalize_int(item.get("updated_at_ms")),
                "recovery_attempted": bool(item.get("recovery_attempted", False)),
            }
        )
        if len(normalized) >= max(limit, 1):
            break
    return normalized


def normalize_context_state(
    payload: Dict[str, Any] | None,
    context: Optional[str] = "default",
    *,
    limits: Any = None,
) -> Dict[str, Any]:
    state = default_context_state(context, limits=limits)
    if not isinstance(payload, dict):
        return state

    state["schema_version"] = _normalize_int(payload.get("schema_version"), 1)
    state["context_id"] = normalize_context_id(payload.get("context_id") or context)
    state["current_capture_file_id"] = str(payload.get("current_capture_file_id") or "").strip()
    state["current_session_id"] = str(payload.get("current_session_id") or "").strip()
    state["entry_mode"] = str(payload.get("entry_mode") or "cli").strip() or "cli"
    state["backend"] = str(payload.get("backend") or "local").strip() or "local"
    state["runtime_parallelism_ceiling"] = (
        str(payload.get("runtime_parallelism_ceiling") or "").strip()
        or ("single_runtime_owner" if state["backend"] == "remote" else "multi_context_multi_owner")
    )

    captures = payload.get("captures")
    if isinstance(captures, dict):
        state["captures"] = {
            str(capture_file_id): _normalize_capture_record(str(capture_file_id), item)
            for capture_file_id, item in captures.items()
            if str(capture_file_id).strip()
        }

    sessions = payload.get("sessions")
    if isinstance(sessions, dict):
        state["sessions"] = {
            str(session_id): _normalize_session_record(str(session_id), item)
            for session_id, item in sessions.items()
            if str(session_id).strip()
        }

    state["runtime_owner"] = _normalize_runtime_owner(payload.get("runtime_owner"))
    state["owner_lease"] = _normalize_runtime_owner(payload.get("owner_lease"))
    if not state["owner_lease"].get("lease_id") and state["runtime_owner"].get("lease_id"):
        state["owner_lease"] = dict(state["runtime_owner"])
    state["active_baton"] = _normalize_active_baton(payload.get("active_baton"))
    state["rehydrate_status"] = _normalize_rehydrate_status(payload.get("rehydrate_status"))

    recovery = payload.get("recovery")
    if isinstance(recovery, dict):
        state["recovery"] = {
            "status": str(recovery.get("status") or "idle").strip() or "idle",
            "last_scan_ms": _normalize_int(recovery.get("last_scan_ms")),
            "last_attempt_ms": _normalize_int(recovery.get("last_attempt_ms")),
            "last_success_ms": _normalize_int(recovery.get("last_success_ms")),
            "attempt_count": _normalize_int(recovery.get("attempt_count")),
            "recovered_session_ids": [str(item).strip() for item in recovery.get("recovered_session_ids", []) if str(item).strip()],
            "degraded_session_ids": [str(item).strip() for item in recovery.get("degraded_session_ids", []) if str(item).strip()],
            "last_error": str(recovery.get("last_error") or "").strip(),
        }

    payload_limits = payload.get("limits")
    if isinstance(payload_limits, dict):
        merged_limits = dict(state["limits"])
        for key, value in payload_limits.items():
            merged_limits[str(key)] = value
        state["limits"] = merged_limits

    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        durations = metrics.get("recent_operation_duration_ms")
        if isinstance(durations, list):
            duration_list = [_normalize_int(item) for item in durations][-64:]
        else:
            duration_list = []
        state["metrics"] = {
            "operation_count": _normalize_int(metrics.get("operation_count")),
            "operation_error_count": _normalize_int(metrics.get("operation_error_count")),
            "recovery_attempt_count": _normalize_int(metrics.get("recovery_attempt_count")),
            "recovery_success_count": _normalize_int(metrics.get("recovery_success_count")),
            "recovery_failure_count": _normalize_int(metrics.get("recovery_failure_count")),
            "rejection_count": _normalize_int(metrics.get("rejection_count")),
            "active_session_count": _normalize_int(metrics.get("active_session_count")),
            "active_capture_count": _normalize_int(metrics.get("active_capture_count")),
            "active_operation_count": _normalize_int(metrics.get("active_operation_count")),
            "recent_operation_duration_ms": duration_list,
            "last_operation_ms": _normalize_int(metrics.get("last_operation_ms")),
            "last_recovery_ms": _normalize_int(metrics.get("last_recovery_ms")),
        }

    recent_operations = payload.get("recent_operations")
    if isinstance(recent_operations, list):
        limit = _normalize_int(state["limits"].get("max_recent_operations"), 64)
        state["recent_operations"] = _trim_recent_operations(recent_operations, limit=limit)

    state["updated_at_ms"] = _normalize_int(payload.get("updated_at_ms"), _now_ms())
    state["created_at_ms"] = _normalize_int(payload.get("created_at_ms"), state["updated_at_ms"])

    if state["current_capture_file_id"] and state["current_capture_file_id"] not in state["captures"]:
        state["current_capture_file_id"] = ""
    if state["current_session_id"] and state["current_session_id"] not in state["sessions"]:
        state["current_session_id"] = ""

    return state


def load_context_state(
    context: Optional[str] = "default",
    *,
    limits: Any = None,
) -> Dict[str, Any]:
    path = context_state_path(context)
    if not path.is_file():
        return default_context_state(context, limits=limits)
    with _locked_context_state_file(context):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default_context_state(context, limits=limits)
    return normalize_context_state(payload, context, limits=limits)


def save_context_state(
    payload: Dict[str, Any],
    context: Optional[str] = "default",
    *,
    limits: Any = None,
) -> Dict[str, Any]:
    state = normalize_context_state(payload, context, limits=limits)
    state["updated_at_ms"] = _now_ms()
    path = context_state_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked_context_state_file(context):
        atomic_write_json(path, state)
    return state


def clear_context_state(context: Optional[str] = "default") -> None:
    with _locked_context_state_file(context):
        try:
            context_state_path(context).unlink(missing_ok=True)
        except Exception:
            pass


def list_context_state_paths() -> list[Path]:
    state_dir = cli_runtime_dir()
    if not state_dir.is_dir():
        return []
    return sorted(path for path in state_dir.glob("runtime_state*.json") if path.is_file())


def list_context_ids() -> list[str]:
    ids: list[str] = []
    for path in list_context_state_paths():
        name = path.name
        if name == "runtime_state.json":
            ids.append("default")
            continue
        prefix = "runtime_state_"
        suffix = ".json"
        if name.startswith(prefix) and name.endswith(suffix):
            ids.append(name[len(prefix) : -len(suffix)] or "default")
    return ids


def append_runtime_log(context: Optional[str], entry: Dict[str, Any]) -> None:
    path = context_log_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_append_jsonl(path, dict(entry or {}))


def read_runtime_logs(context: Optional[str], *, since_ms: Optional[int] = None, max_lines: int = 500) -> list[Dict[str, Any]]:
    path = context_log_path(context)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[Dict[str, Any]] = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        if since_ms is not None and _normalize_int(item.get("ts_ms")) < int(since_ms):
            continue
        out.append(item)
    return out[-max(max_lines, 1) :]


def percentile(values: Iterable[int], pct: float) -> float:
    items = sorted(int(v) for v in values)
    if not items:
        return 0.0
    if len(items) == 1:
        return float(items[0])
    rank = max(0.0, min(1.0, float(pct))) * float(len(items) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(items) - 1)
    if lo == hi:
        return float(items[lo])
    frac = rank - float(lo)
    return float(items[lo]) * (1.0 - frac) + float(items[hi]) * frac


def summarize_operation_durations(values: Iterable[int]) -> Dict[str, float]:
    items = [int(v) for v in values if int(v) >= 0]
    if not items:
        return {"count": 0.0, "avg_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    return {
        "count": float(len(items)),
        "avg_ms": float(sum(items)) / float(len(items)),
        "p95_ms": percentile(items, 0.95),
        "max_ms": float(max(items)),
    }
