"""Persistent worker state helpers for daemon-owned RenderDoc workers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from rdx.context_snapshot import normalize_context_id
from rdx.io_utils import atomic_write_json
from rdx.runtime_paths import worker_state_dir


def _sanitize_context(context: Optional[str]) -> str:
    ctx = normalize_context_id(context)
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in ctx)


def worker_state_path(context: Optional[str] = "default") -> Path:
    root = worker_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    ctx = normalize_context_id(context)
    if ctx == "default":
        return root / "worker_state.json"
    return root / f"worker_state_{_sanitize_context(ctx)}.json"


def list_worker_state_paths() -> Iterable[Path]:
    root = worker_state_dir()
    if not root.is_dir():
        return []
    return sorted(root.glob("worker_state*.json"))


def load_worker_state(context: Optional[str] = "default") -> Dict[str, Any]:
    path = worker_state_path(context)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def save_worker_state(payload: Dict[str, Any], context: Optional[str] = "default") -> None:
    path = worker_state_path(context)
    atomic_write_json(path, dict(payload or {}))


def clear_worker_state(context: Optional[str] = "default") -> None:
    try:
        worker_state_path(context).unlink(missing_ok=True)
    except Exception:
        pass
