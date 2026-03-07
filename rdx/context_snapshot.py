"""Context-scoped snapshot helpers shared by runtime and daemon-facing tools."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from rdx.runtime_paths import cli_runtime_dir

MAX_RECENT_ARTIFACTS = 8
USER_CONTEXT_KEYS = {
    "focus_pixel",
    "focus_resource_id",
    "focus_shader_id",
    "notes",
}
RUNTIME_OWNED_KEYS = {
    "session_id",
    "capture_file_id",
    "frame_index",
    "active_event_id",
    "backend_type",
    "remote_id",
    "origin_remote_id",
    "last_artifacts",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def normalize_context_id(context: Optional[str]) -> str:
    text = str(context or "").strip()
    if not text or text.lower() == "default":
        return "default"
    return text


def _sanitize_context(context: Optional[str]) -> str:
    ctx = normalize_context_id(context)
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in ctx)


def context_snapshot_path(context: Optional[str] = "default") -> Path:
    state_dir = cli_runtime_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    ctx = normalize_context_id(context)
    if ctx == "default":
        return state_dir / "context_snapshot.json"
    return state_dir / f"context_snapshot_{_sanitize_context(ctx)}.json"


def default_context_snapshot(context: Optional[str] = "default") -> Dict[str, Any]:
    ctx = normalize_context_id(context)
    return {
        "context_id": ctx,
        "runtime": {
            "session_id": "",
            "capture_file_id": "",
            "frame_index": 0,
            "active_event_id": 0,
            "backend_type": "none",
        },
        "remote": {
            "state": "none",
            "remote_id": "",
            "origin_remote_id": "",
            "endpoint": "",
            "consumed_by_session_id": "",
        },
        "focus": {
            "pixel": None,
            "resource_id": "",
            "shader_id": "",
        },
        "notes": "",
        "last_artifacts": [],
        "updated_at_ms": _now_ms(),
    }


def _normalize_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed


def normalize_pixel(value: Any) -> Dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        if len(parts) >= 2:
            try:
                return {"x": int(parts[0]), "y": int(parts[1])}
            except ValueError:
                return None
        return None
    if isinstance(value, dict):
        if "x" not in value or "y" not in value:
            return None
        try:
            payload: Dict[str, Any] = {
                "x": int(value.get("x")),
                "y": int(value.get("y")),
            }
        except (TypeError, ValueError):
            return None
        for key in ("sample", "view", "primitive"):
            if key in value and value.get(key) is not None:
                payload[key] = _normalize_int(value.get(key))
        target = value.get("target")
        if isinstance(target, dict):
            payload["target"] = dict(target)
        return payload
    return None


def _normalize_artifact_entry(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    path = str(value.get("path") or "").strip()
    if not path:
        return None
    return {
        "path": path,
        "type": str(value.get("type") or "file").strip() or "file",
        "source_tool": str(value.get("source_tool") or "").strip(),
        "ts_ms": _normalize_int(value.get("ts_ms"), _now_ms()),
    }


def normalize_context_snapshot(payload: Dict[str, Any] | None, context: Optional[str] = "default") -> Dict[str, Any]:
    snapshot = default_context_snapshot(context)
    if not isinstance(payload, dict):
        return snapshot

    snapshot["context_id"] = normalize_context_id(payload.get("context_id") or context)

    runtime_payload = payload.get("runtime")
    if isinstance(runtime_payload, dict):
        snapshot["runtime"]["session_id"] = str(runtime_payload.get("session_id") or "").strip()
        snapshot["runtime"]["capture_file_id"] = str(runtime_payload.get("capture_file_id") or "").strip()
        snapshot["runtime"]["frame_index"] = _normalize_int(runtime_payload.get("frame_index"))
        snapshot["runtime"]["active_event_id"] = _normalize_int(runtime_payload.get("active_event_id"))
        snapshot["runtime"]["backend_type"] = str(runtime_payload.get("backend_type") or "none").strip() or "none"

    remote_payload = payload.get("remote")
    if isinstance(remote_payload, dict):
        snapshot["remote"]["state"] = str(remote_payload.get("state") or "none").strip() or "none"
        snapshot["remote"]["remote_id"] = str(remote_payload.get("remote_id") or "").strip()
        snapshot["remote"]["origin_remote_id"] = str(remote_payload.get("origin_remote_id") or "").strip()
        snapshot["remote"]["endpoint"] = str(remote_payload.get("endpoint") or "").strip()
        snapshot["remote"]["consumed_by_session_id"] = str(
            remote_payload.get("consumed_by_session_id") or ""
        ).strip()

    focus_payload = payload.get("focus")
    if isinstance(focus_payload, dict):
        snapshot["focus"]["pixel"] = normalize_pixel(focus_payload.get("pixel"))
        snapshot["focus"]["resource_id"] = str(focus_payload.get("resource_id") or "").strip()
        snapshot["focus"]["shader_id"] = str(focus_payload.get("shader_id") or "").strip()

    snapshot["notes"] = str(payload.get("notes") or "").strip()

    artifacts = payload.get("last_artifacts")
    if isinstance(artifacts, list):
        normalized = []
        for item in artifacts:
            entry = _normalize_artifact_entry(item)
            if entry is not None:
                normalized.append(entry)
        snapshot["last_artifacts"] = normalized[:MAX_RECENT_ARTIFACTS]

    snapshot["updated_at_ms"] = _normalize_int(payload.get("updated_at_ms"), _now_ms())
    return snapshot


def load_context_snapshot(context: Optional[str] = "default") -> Dict[str, Any]:
    path = context_snapshot_path(context)
    if not path.is_file():
        return default_context_snapshot(context)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_context_snapshot(context)
    return normalize_context_snapshot(payload, context)


def save_context_snapshot(payload: Dict[str, Any], context: Optional[str] = "default") -> Dict[str, Any]:
    snapshot = normalize_context_snapshot(payload, context)
    snapshot["updated_at_ms"] = _now_ms()
    path = context_snapshot_path(context)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


def clear_context_snapshot(context: Optional[str] = "default") -> None:
    try:
        context_snapshot_path(context).unlink(missing_ok=True)
    except Exception:
        pass


def update_user_context(snapshot: Dict[str, Any], key: str, value: Any) -> Dict[str, Any]:
    if key not in USER_CONTEXT_KEYS:
        if key in RUNTIME_OWNED_KEYS:
            raise ValueError(f"Context key '{key}' is runtime-owned and cannot be updated manually")
        raise ValueError(f"Unsupported context key: {key}")

    updated = normalize_context_snapshot(snapshot, snapshot.get("context_id"))
    if key == "focus_pixel":
        updated["focus"]["pixel"] = normalize_pixel(value)
    elif key == "focus_resource_id":
        updated["focus"]["resource_id"] = str(value or "").strip()
    elif key == "focus_shader_id":
        updated["focus"]["shader_id"] = str(value or "").strip()
    elif key == "notes":
        updated["notes"] = str(value or "").strip()
    updated["updated_at_ms"] = _now_ms()
    return updated


def merge_recent_artifacts(
    snapshot: Dict[str, Any],
    artifacts: Iterable[Dict[str, Any]],
    *,
    source_tool: str,
) -> Dict[str, Any]:
    updated = normalize_context_snapshot(snapshot, snapshot.get("context_id"))
    current = [
        entry
        for entry in (updated.get("last_artifacts") or [])
        if isinstance(entry, dict) and str(entry.get("path") or "").strip()
    ]
    ts_ms = _now_ms()
    for artifact in artifacts:
        path = str(artifact.get("path") or "").strip()
        if not path:
            continue
        entry = {
            "path": path,
            "type": str(artifact.get("type") or "file").strip() or "file",
            "source_tool": source_tool,
            "ts_ms": ts_ms,
        }
        current = [item for item in current if str(item.get("path") or "").strip() != path]
        current.insert(0, entry)
    updated["last_artifacts"] = current[:MAX_RECENT_ARTIFACTS]
    updated["updated_at_ms"] = _now_ms()
    return updated
