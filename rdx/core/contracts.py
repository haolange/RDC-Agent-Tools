"""Canonical response and artifact contracts for unified core execution."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from rdx import __version__ as _TOOL_VERSION

SCHEMA_VERSION = "2.0.0"
TSV_FORMAT_VERSION = "1.0.0"


def now_ms() -> int:
    return int(time.time() * 1000)


def tool_version() -> str:
    return str(_TOOL_VERSION)


def _guess_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _artifact_id(seed: str) -> str:
    return f"art_{hashlib.sha1(seed.encode('utf-8', errors='ignore')).hexdigest()[:12]}"


def make_artifact_from_path(
    path: str,
    *,
    artifact_type: str = "file",
    metadata: Optional[Dict[str, Any]] = None,
    storage_backend: str = "local",
    url: Optional[str] = None,
) -> Dict[str, Any]:
    p = Path(path)
    size_bytes = 0
    sha256 = ""
    mime = "application/octet-stream"
    if p.exists() and p.is_file():
        size_bytes = int(p.stat().st_size)
        sha256 = _sha256_file(p)
        mime = _guess_mime(p)
    return {
        "artifact_id": _artifact_id(url or str(p)),
        "type": artifact_type,
        "mime": mime,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "path": str(p),
        "url": url,
        "storage_backend": storage_backend,
        "metadata": dict(metadata or {}),
    }


def make_artifact_from_url(
    url: str,
    *,
    artifact_type: str = "file",
    metadata: Optional[Dict[str, Any]] = None,
    storage_backend: str = "s3",
    mime: str = "application/octet-stream",
    size_bytes: int = 0,
    sha256: str = "",
) -> Dict[str, Any]:
    return {
        "artifact_id": _artifact_id(url),
        "type": artifact_type,
        "mime": mime,
        "size_bytes": int(size_bytes),
        "sha256": str(sha256),
        "path": None,
        "url": str(url),
        "storage_backend": storage_backend,
        "metadata": dict(metadata or {}),
    }


def canonical_success(
    *,
    result_kind: str,
    data: Optional[Dict[str, Any]] = None,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    meta: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    transport: str = "core",
    duration_ms: Optional[int] = None,
) -> Dict[str, Any]:
    out_meta = dict(meta or {})
    if trace_id:
        out_meta.setdefault("trace_id", trace_id)
    out_meta.setdefault("transport", transport)
    if duration_ms is not None:
        out_meta.setdefault("duration_ms", int(duration_ms))
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": tool_version(),
        "result_kind": str(result_kind),
        "ok": True,
        "success": True,
        "data": dict(data or {}),
        "artifacts": list(artifacts or []),
        "error": None,
        "meta": out_meta,
    }
    # Backward-compat shadow fields for existing consumers.
    payload.update(dict(data or {}))
    return payload


def canonical_error(
    *,
    result_kind: str,
    code: str,
    message: str,
    category: str = "internal",
    details: Optional[Dict[str, Any]] = None,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    meta: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    transport: str = "core",
    duration_ms: Optional[int] = None,
) -> Dict[str, Any]:
    out_meta = dict(meta or {})
    if trace_id:
        out_meta.setdefault("trace_id", trace_id)
    out_meta.setdefault("transport", transport)
    if duration_ms is not None:
        out_meta.setdefault("duration_ms", int(duration_ms))
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": tool_version(),
        "result_kind": str(result_kind),
        "ok": False,
        "success": False,
        "error_message": str(message),
        "data": {},
        "artifacts": list(artifacts or []),
        "error": {
            "code": str(code),
            "category": str(category),
            "message": str(message),
            "details": dict(details or {}),
        },
        "meta": out_meta,
    }
    return payload


_PATH_KEYS = {
    "artifact_path",
    "saved_path",
    "image_path",
    "diff_path",
    "values_path",
    "report_path",
}

_PATH_LIST_KEYS = {"saved_paths"}
_URL_KEYS = {"artifact_url", "details_json_url", "url"}


def _append_path_candidate(candidates: List[Dict[str, Any]], value: Any, key: str) -> None:
    text = str(value or "").strip()
    if not text:
        return
    # Avoid accidentally treating URI-ish values as local paths.
    if "://" in text:
        candidates.append({"url": text, "type": key, "metadata": {}})
        return
    candidates.append({"path": text, "type": key, "metadata": {}})


def collect_artifact_candidates(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract artifact references from legacy payload fields."""
    candidates: List[Dict[str, Any]] = []
    for key in _PATH_KEYS:
        if key in payload:
            _append_path_candidate(candidates, payload.get(key), key)
    for key in _URL_KEYS:
        if key in payload:
            text = str(payload.get(key) or "").strip()
            if text:
                candidates.append({"url": text, "type": key, "metadata": {}})
    for key in _PATH_LIST_KEYS:
        seq = payload.get(key)
        if isinstance(seq, list):
            for item in seq:
                _append_path_candidate(candidates, item, key)

    exports = payload.get("exports")
    if isinstance(exports, list):
        for item in exports:
            if not isinstance(item, dict):
                continue
            if "saved_path" in item:
                _append_path_candidate(candidates, item.get("saved_path"), "export_saved_path")
            if "artifact_path" in item:
                _append_path_candidate(candidates, item.get("artifact_path"), "export_artifact_path")
            if "url" in item:
                text = str(item.get("url") or "").strip()
                if text:
                    candidates.append({"url": text, "type": "export_url", "metadata": {}})
    return candidates


def stable_tsv_header(columns: Iterable[str]) -> List[str]:
    cols = [str(c) for c in columns]
    out: List[str] = ["format_version"]
    for required in ("details_json_path", "details_json_url"):
        if required not in cols:
            cols.append(required)
    for c in cols:
        if c not in out:
            out.append(c)
    return out


def normalize_drilldown_fields(row: Dict[str, Any], *, details_path: str = "", details_url: str = "") -> Dict[str, Any]:
    out = dict(row)
    out.setdefault("details_json_path", details_path)
    out.setdefault("details_json_url", details_url)
    return out


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}
