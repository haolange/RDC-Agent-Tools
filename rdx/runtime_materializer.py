"""Stage RenderDoc runtime binaries into worker-private cache roots."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from rdx.io_utils import atomic_swap_path, atomic_write_json
from rdx.runtime_paths import binaries_root, pymodules_dir, worker_cache_dir


@dataclass(frozen=True)
class RuntimeSource:
    source_root: Path
    binaries_dir: Path
    pymodules_dir: Path
    manifest_path: Path
    manifest_sha256: str
    materialized_manifest_sha256: str
    files: List[Dict[str, Any]]
    materialized_files: List[Dict[str, Any]]
    bundled_python: Dict[str, Any]


@dataclass(frozen=True)
class MaterializedRuntime:
    runtime_id: str
    source_manifest: Path
    cache_root: Path
    binaries_dir: Path
    pymodules_dir: Path
    manifest_sha256: str


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _abi_fingerprint() -> str:
    cache_tag = str(getattr(sys.implementation, "cache_tag", "") or "unknown")
    machine = str(platform.machine() or "unknown")
    system = str(platform.system() or "unknown")
    release = str(platform.release() or "unknown")
    return "|".join(
        [
            cache_tag,
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            f"{system}-{release}",
            machine,
            os.name,
        ]
    )


def _materialized_entries(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    entries = [dict(entry) for entry in files if bool(entry.get("worker_materialize", True))]
    if not entries:
        raise RuntimeError("runtime manifest does not include any worker-materialized files")
    return entries


def _validate_manifest_entries(bin_dir: Path, files: List[Dict[str, Any]]) -> None:
    for entry in files:
        if not isinstance(entry, dict):
            raise RuntimeError("runtime manifest entry is not an object")
        rel = str(entry.get("path") or "").strip()
        if not rel:
            raise RuntimeError("runtime manifest contains an empty path")
        file_path = (bin_dir / rel).resolve()
        try:
            file_path.relative_to(bin_dir)
        except ValueError as exc:
            raise RuntimeError(f"runtime manifest escapes binaries root: {rel}") from exc
        if not file_path.is_file():
            raise RuntimeError(f"runtime manifest references missing file: {file_path}")


def _materialized_manifest_sha256(entries: List[Dict[str, Any]]) -> str:
    payload = json.dumps(entries, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(payload)


def load_runtime_source() -> RuntimeSource:
    bin_dir = binaries_root().resolve()
    pymod_dir = pymodules_dir().resolve()
    manifest_path = (bin_dir / "manifest.runtime.json").resolve()
    if not manifest_path.is_file():
        raise RuntimeError(f"missing runtime manifest: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError(f"runtime manifest is missing files: {manifest_path}")
    normalized_files = [dict(entry) for entry in files]
    _validate_manifest_entries(bin_dir, normalized_files)
    materialized_files = _materialized_entries(normalized_files)
    materialized_paths = {str(entry.get("path") or "").strip().replace("\\", "/") for entry in materialized_files}
    if "renderdoc.dll" not in materialized_paths:
        raise RuntimeError("runtime manifest is missing worker-materialized renderdoc.dll")
    if "pymodules/renderdoc.pyd" not in materialized_paths:
        raise RuntimeError("runtime manifest is missing worker-materialized pymodules/renderdoc.pyd")
    return RuntimeSource(
        source_root=bin_dir.parent.parent.parent.resolve(),
        binaries_dir=bin_dir,
        pymodules_dir=pymod_dir,
        manifest_path=manifest_path,
        manifest_sha256=_sha256_file(manifest_path),
        materialized_manifest_sha256=_materialized_manifest_sha256(materialized_files),
        files=normalized_files,
        materialized_files=materialized_files,
        bundled_python=dict(payload.get("bundled_python") or {}),
    )


def compute_runtime_id(source: RuntimeSource) -> str:
    seed = json.dumps(
        {
            "manifest_sha256": source.materialized_manifest_sha256,
            "abi": _abi_fingerprint(),
        },
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")
    return _sha256_bytes(seed)[:16]


def _copy_runtime_tree(source: RuntimeSource, target_root: Path) -> None:
    target_root.mkdir(parents=True, exist_ok=True)
    for entry in source.materialized_files:
        rel = str(entry.get("path") or "").strip()
        src = source.binaries_dir / rel
        dst = target_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    filtered_manifest = {
        "file_count": len(source.materialized_files),
        "files": source.materialized_files,
    }
    if source.bundled_python:
        filtered_manifest["bundled_python"] = source.bundled_python
    (target_root / "manifest.runtime.json").write_text(
        json.dumps(filtered_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def materialize_runtime() -> MaterializedRuntime:
    source = load_runtime_source()
    runtime_id = compute_runtime_id(source)
    cache_root = worker_cache_dir().resolve() / runtime_id
    marker_path = cache_root / ".materialized.json"
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception:
            marker = {}
        if (
            str(marker.get("runtime_id") or "").strip() == runtime_id
            and str(marker.get("manifest_sha256") or "").strip() == source.materialized_manifest_sha256
            and (cache_root / "renderdoc.dll").is_file()
            and (cache_root / "pymodules" / "renderdoc.pyd").is_file()
        ):
            return MaterializedRuntime(
                runtime_id=runtime_id,
                source_manifest=source.manifest_path,
                cache_root=cache_root,
                binaries_dir=cache_root,
                pymodules_dir=cache_root / "pymodules",
                manifest_sha256=source.materialized_manifest_sha256,
            )

    worker_cache_dir().mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f"rt-{runtime_id}-", dir=str(worker_cache_dir().resolve())))
    try:
        _copy_runtime_tree(source, temp_root)
        marker = {
            "runtime_id": runtime_id,
            "manifest_sha256": source.materialized_manifest_sha256,
            "source_manifest": str(source.manifest_path),
            "cache_root": str(cache_root),
        }
        atomic_write_json(temp_root / ".materialized.json", marker)
        atomic_swap_path(temp_root, cache_root)
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)

    return MaterializedRuntime(
        runtime_id=runtime_id,
        source_manifest=source.manifest_path,
        cache_root=cache_root,
        binaries_dir=cache_root,
        pymodules_dir=cache_root / "pymodules",
        manifest_sha256=source.materialized_manifest_sha256,
    )