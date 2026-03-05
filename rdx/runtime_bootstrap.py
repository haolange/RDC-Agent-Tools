"""RenderDoc runtime bootstrap helpers for standalone rdx-tools."""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

from rdx.runtime_paths import binaries_root, pymodules_dir

_DLL_DIR_HANDLES: List[Any] = []
_REGISTERED_DLL_DIRS: set[str] = set()


@dataclass
class RuntimeBootstrapResult:
    binaries_dir: Path
    pymodules_dir: Path
    sys_path_added: bool = False
    path_prepended: bool = False
    dll_dirs_registered: list[str] = field(default_factory=list)
    dll_dir_errors: list[str] = field(default_factory=list)
    import_ok: bool = False
    import_module_path: str = ""
    import_error: str = ""


def _normalize_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _same_path(left: str, right: str) -> bool:
    try:
        return _normalize_path(left) == _normalize_path(right)
    except Exception:
        return left.strip().lower() == right.strip().lower()


def _resolve_runtime_dirs() -> tuple[Path, Path]:
    default_bin = binaries_root().resolve()
    default_pymod = pymodules_dir().resolve()

    env_bin = os.environ.get("RDX_RUNTIME_DLL_DIR", "").strip()
    env_pymod = os.environ.get("RDX_RENDERDOC_PATH", "").strip()

    bin_dir = _normalize_path(env_bin) if env_bin else default_bin
    pymod_dir = _normalize_path(env_pymod) if env_pymod else default_pymod

    if env_bin and not env_pymod:
        pymod_dir = (bin_dir / "pymodules").resolve()
    elif env_pymod and not env_bin and pymod_dir.name.lower() == "pymodules":
        bin_dir = pymod_dir.parent.resolve()

    return bin_dir, pymod_dir


def _prepend_path(path: Path) -> bool:
    candidate = str(path)
    current = os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    if any(_same_path(p, candidate) for p in parts):
        return False
    os.environ["PATH"] = f"{candidate}{os.pathsep}{current}" if current else candidate
    return True


def _register_dll_directory(path: Path, report: RuntimeBootstrapResult) -> None:
    if os.name != "nt":
        return
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if not callable(add_dll_directory):
        return
    if not path.is_dir():
        report.dll_dir_errors.append(f"missing directory: {path}")
        return

    key = str(path.resolve()).lower()
    if key in _REGISTERED_DLL_DIRS:
        return

    try:
        handle = add_dll_directory(str(path))
        _DLL_DIR_HANDLES.append(handle)
        _REGISTERED_DLL_DIRS.add(key)
        report.dll_dirs_registered.append(str(path))
    except Exception as exc:  # noqa: BLE001
        report.dll_dir_errors.append(f"{path}: {exc.__class__.__name__}: {exc}")


def _probe_renderdoc_import(report: RuntimeBootstrapResult) -> None:
    try:
        importlib.invalidate_caches()
        import renderdoc as rd  # type: ignore[import-not-found]

        report.import_ok = True
        report.import_module_path = str(getattr(rd, "__file__", ""))
    except Exception as exc:  # noqa: BLE001
        report.import_ok = False
        report.import_error = f"{exc.__class__.__name__}: {exc}"


def bootstrap_renderdoc_runtime(*, probe_import: bool = False) -> RuntimeBootstrapResult:
    """Prepare Python/runtime environment for loading renderdoc.pyd."""

    bin_dir, pymod_dir = _resolve_runtime_dirs()
    report = RuntimeBootstrapResult(
        binaries_dir=bin_dir,
        pymodules_dir=pymod_dir,
    )

    os.environ["RDX_RUNTIME_DLL_DIR"] = str(bin_dir)
    os.environ["RDX_RENDERDOC_PATH"] = str(pymod_dir)

    pymod_text = str(pymod_dir)
    if pymod_text not in sys.path:
        sys.path.insert(0, pymod_text)
        report.sys_path_added = True

    report.path_prepended = _prepend_path(bin_dir)

    _register_dll_directory(bin_dir, report)
    _register_dll_directory(pymod_dir, report)

    if probe_import:
        _probe_renderdoc_import(report)

    return report
