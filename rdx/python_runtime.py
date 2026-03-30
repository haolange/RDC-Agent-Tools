"""Helpers for the bundled Windows Python runtime shipped with rdx-tools."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rdx.runtime_paths import binaries_root


@dataclass(frozen=True)
class BundledPythonLayout:
    root: Path
    metadata: dict[str, Any]
    python_exe: Path
    pythonw_exe: Path
    python3_dll: Path
    python_dll: Path
    stdlib_path: Path
    site_packages_dir: Path
    dll_dir: Path
    pth_file: Path


def runtime_manifest_path() -> Path:
    return binaries_root() / "manifest.runtime.json"


def bundled_python_root() -> Path:
    return binaries_root() / "python"


def bundled_python_executable() -> Path:
    return bundled_python_root() / "python.exe"


def _load_manifest() -> dict[str, Any]:
    path = runtime_manifest_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_path_from_binaries(raw_path: str, *, label: str) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise RuntimeError(f"bundled_python metadata is missing {label}")
    root = binaries_root().resolve()
    path = (root / text).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"bundled_python metadata escapes binaries root: {label}={text}") from exc
    return path


def resolve_bundled_python_layout() -> BundledPythonLayout:
    manifest = _load_manifest()
    metadata = dict(manifest.get("bundled_python") or {})
    defaults = {
        "python_entry": "python/python.exe",
        "pythonw_entry": "python/pythonw.exe",
        "python3_dll": "python/python3.dll",
        "python_dll": "python/python314.dll",
        "stdlib_layout": "python/Lib",
        "site_packages": "python/Lib/site-packages",
        "dll_dir": "python/DLLs",
        "pth_file": "python/python314._pth",
    }
    effective = dict(defaults)
    for key, value in metadata.items():
        if isinstance(value, str) and value.strip():
            effective[key] = value.strip()
    return BundledPythonLayout(
        root=bundled_python_root().resolve(),
        metadata=metadata,
        python_exe=_resolve_path_from_binaries(effective["python_entry"], label="python_entry"),
        pythonw_exe=_resolve_path_from_binaries(effective["pythonw_entry"], label="pythonw_entry"),
        python3_dll=_resolve_path_from_binaries(effective["python3_dll"], label="python3_dll"),
        python_dll=_resolve_path_from_binaries(effective["python_dll"], label="python_dll"),
        stdlib_path=_resolve_path_from_binaries(effective["stdlib_layout"], label="stdlib_layout"),
        site_packages_dir=_resolve_path_from_binaries(effective["site_packages"], label="site_packages"),
        dll_dir=_resolve_path_from_binaries(effective["dll_dir"], label="dll_dir"),
        pth_file=_resolve_path_from_binaries(effective["pth_file"], label="pth_file"),
    )


def current_python_runtime_details() -> dict[str, str]:
    return {
        "executable": str(Path(sys.executable).resolve()),
        "version": sys.version.split()[0],
        "prefix": str(Path(sys.prefix).resolve()),
        "base_prefix": str(Path(sys.base_prefix).resolve()),
    }


def validate_bundled_python_layout() -> tuple[bool, list[str], dict[str, Any]]:
    manifest = _load_manifest()
    metadata = dict(manifest.get("bundled_python") or {})
    required_metadata_keys = (
        "python_version",
        "python_entry",
        "pythonw_entry",
        "python3_dll",
        "python_dll",
        "stdlib_layout",
        "site_packages",
        "dll_dir",
        "pth_file",
    )
    failures: list[str] = []
    for key in required_metadata_keys:
        if not str(metadata.get(key) or "").strip():
            failures.append(f"bundled_python metadata missing {key}")

    try:
        layout = resolve_bundled_python_layout()
    except Exception as exc:
        failures.append(str(exc))
        return False, failures, {"bundled_python": metadata, "current_python": current_python_runtime_details()}

    required_paths = {
        "root": layout.root,
        "python_exe": layout.python_exe,
        "pythonw_exe": layout.pythonw_exe,
        "python3_dll": layout.python3_dll,
        "python_dll": layout.python_dll,
        "site_packages_dir": layout.site_packages_dir,
        "dll_dir": layout.dll_dir,
        "pth_file": layout.pth_file,
    }
    for label, path in required_paths.items():
        if label.endswith("_dir") or label == "root":
            if not path.is_dir():
                failures.append(f"missing bundled python directory: {label}={path}")
        elif not path.is_file():
            failures.append(f"missing bundled python file: {label}={path}")

    if layout.stdlib_path.suffix.lower() == ".zip":
        if not layout.stdlib_path.is_file():
            failures.append(f"missing bundled python stdlib: {layout.stdlib_path}")
    else:
        if not layout.stdlib_path.is_dir():
            failures.append(f"missing bundled python stdlib dir: {layout.stdlib_path}")
        else:
            for marker in ("os.py", "site.py", "encodings\\__init__.py"):
                if not (layout.stdlib_path / marker).is_file():
                    failures.append(f"missing stdlib marker: {layout.stdlib_path / marker}")

    details = {
        "bundled_python": {
            "root": str(layout.root),
            "python_version": str(metadata.get("python_version") or ""),
            "python_entry": str(layout.python_exe),
            "pythonw_entry": str(layout.pythonw_exe),
            "python3_dll": str(layout.python3_dll),
            "python_dll": str(layout.python_dll),
            "stdlib_layout": str(layout.stdlib_path),
            "site_packages": str(layout.site_packages_dir),
            "dll_dir": str(layout.dll_dir),
            "pth_file": str(layout.pth_file),
        },
        "current_python": current_python_runtime_details(),
    }
    return len(failures) == 0, failures, details