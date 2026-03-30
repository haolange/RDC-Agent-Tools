#!/usr/bin/env python3
"""Copy runtime binaries into rdx-tools/binaries and generate manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from rdx.runtime_requirements import should_bundle_site_package
from scripts._shared import ensure_within_root, resolve_repo_path, tools_root, write_text


DENY_SUFFIXES = {".pdb", ".lib", ".exp", ".ilk", ".h"}
ALLOW_PATTERNS = (
    "*.dll",
    "*.json",
)
ALLOW_PYMODULE_PATTERNS = (
    "*.pyd",
    "*.dll",
    "*.json",
)
SKIP_STDLIB_TOP_LEVEL = {"site-packages", "test", "tests", "idlelib", "turtledemo", "ensurepip", "venv", "__pycache__"}
PYTHON_BASE_FILES = (
    "python.exe",
    "pythonw.exe",
    "python3.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "LICENSE.txt",
)


def _tools_root() -> Path:
    return tools_root(__file__)


def _resolve_input_path(root: Path, raw_path: str, *, label: str, allow_outside_root: bool = False) -> Path:
    resolved = resolve_repo_path(root, raw_path)
    if allow_outside_root:
        return resolved
    return ensure_within_root(root, resolved, label=label)


def _iter_allowed_files(src: Path) -> Iterable[Path]:
    for pattern in ALLOW_PATTERNS:
        for p in sorted(src.glob(pattern)):
            if p.is_file():
                yield p
    pymod = src / "pymodules"
    if pymod.is_dir():
        for pattern in ALLOW_PYMODULE_PATTERNS:
            for p in sorted(pymod.glob(pattern)):
                if p.is_file():
                    yield p


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _copy_tree_filtered(src: Path, dst: Path, *, skip_top_level: set[str] | None = None) -> None:
    skip_top_level = {item.lower() for item in (skip_top_level or set())}
    for path in src.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        parts = [part.lower() for part in rel.parts]
        if any(part == "__pycache__" for part in parts):
            continue
        if path.suffix.lower() in (".pyc", ".md", ".rst", ".chm"):
            continue
        if rel.parts and rel.parts[0].lower() in skip_top_level:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _copy_site_packages(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir()):
        if not should_bundle_site_package(child.name):
            continue
        target = dst / child.name
        if child.is_dir():
            _copy_tree_filtered(child, target)
        else:
            if child.suffix.lower() in (".pyc", ".md", ".rst", ".chm"):
                continue
            shutil.copy2(child, target)


def _probe_python_version(python_home: Path) -> str:
    python_exe = python_home / "python.exe"
    if not python_exe.is_file():
        raise RuntimeError(f"missing python.exe in python home: {python_exe}")
    proc = subprocess.run(
        [str(python_exe), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to probe python version from {python_exe}: {(proc.stdout or '')}{(proc.stderr or '')}".strip())
    return str(proc.stdout or "").strip()


def _python_tag(python_version: str) -> str:
    parts = str(python_version or "").strip().split(".")
    if len(parts) < 2:
        raise RuntimeError(f"invalid python version: {python_version}")
    return f"{parts[0]}{parts[1]}"


def _bundle_python_runtime(root: Path, out_root: Path, python_home_raw: str, site_packages_raw: str | None, python_version_raw: str | None) -> dict[str, str]:
    python_home = _resolve_input_path(root, python_home_raw, label="python home", allow_outside_root=True)
    if not python_home.is_dir():
        raise RuntimeError(f"missing python home: {python_home}")

    site_packages_source: Path | None = None
    if site_packages_raw:
        site_packages_source = _resolve_input_path(root, site_packages_raw, label="site-packages source")
        if not site_packages_source.is_dir():
            raise RuntimeError(f"missing site-packages source: {site_packages_source}")

    python_version = str(python_version_raw or "").strip() or _probe_python_version(python_home)
    tag = _python_tag(python_version)
    python_dir = out_root / "python"
    if site_packages_source is not None:
        site_packages_resolved = site_packages_source.resolve()
        python_dir_resolved = python_dir.resolve()
        try:
            site_packages_resolved.relative_to(python_dir_resolved)
        except ValueError:
            pass
        else:
            raise RuntimeError(
                f"site-packages source must not point inside the bundled python output tree: {site_packages_resolved}"
            )
    if python_dir.exists():
        shutil.rmtree(python_dir)
    python_dir.mkdir(parents=True, exist_ok=True)

    for name in PYTHON_BASE_FILES:
        src = python_home / name
        if src.is_file():
            shutil.copy2(src, python_dir / name)
    versioned_dll = python_home / f"python{tag}.dll"
    if not versioned_dll.is_file():
        raise RuntimeError(f"missing versioned python dll: {versioned_dll}")
    shutil.copy2(versioned_dll, python_dir / versioned_dll.name)

    dll_dir = python_home / "DLLs"
    if dll_dir.is_dir():
        _copy_tree_filtered(dll_dir, python_dir / "DLLs")

    lib_dir = python_home / "Lib"
    if not lib_dir.is_dir():
        raise RuntimeError(f"missing stdlib directory: {lib_dir}")
    _copy_tree_filtered(lib_dir, python_dir / "Lib", skip_top_level=SKIP_STDLIB_TOP_LEVEL)

    bundled_site_packages = python_dir / "Lib" / "site-packages"
    bundled_site_packages.mkdir(parents=True, exist_ok=True)
    if site_packages_source is not None:
        _copy_site_packages(site_packages_source, bundled_site_packages)

    if (bundled_site_packages / "win32").is_dir() and (bundled_site_packages / "pywin32_system32").is_dir():
        write_text(
            bundled_site_packages / "pywin32.pth",
            "win32\nwin32\\lib\npywin32_system32\n",
            encoding="utf-8",
        )

    pth_lines = [
        ".",
        "DLLs",
        "Lib",
        "Lib/site-packages",
        "..\\..\\..\\..",
        "import site",
        "",
    ]
    write_text(python_dir / f"python{tag}._pth", "\n".join(pth_lines), encoding="utf-8")

    return {
        "python_version": python_version,
        "python_entry": "python/python.exe",
        "pythonw_entry": "python/pythonw.exe",
        "python3_dll": "python/python3.dll",
        "python_dll": f"python/python{tag}.dll",
        "stdlib_layout": "python/Lib",
        "site_packages": "python/Lib/site-packages",
        "dll_dir": "python/DLLs",
        "pth_file": f"python/python{tag}._pth",
    }


def _iter_manifest_files(out_root: Path) -> Iterable[tuple[Path, bool]]:
    for path in sorted(out_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(out_root).as_posix()
        if rel == "manifest.runtime.json":
            continue
        if path.suffix.lower() in DENY_SUFFIXES:
            continue
        if rel.startswith("python/"):
            yield path, False
            continue
        if rel.startswith("pymodules/"):
            yield path, True
            continue
        if "/" in rel:
            continue
        if path.name.lower().startswith("python"):
            continue
        yield path, True


def _manifest_entry(path: Path, out_root: Path, *, worker_materialize: bool) -> dict[str, object]:
    rel = path.relative_to(out_root).as_posix()
    return {
        "path": rel,
        "size": int(path.stat().st_size),
        "sha256": _sha256(path),
        "worker_materialize": bool(worker_materialize),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Package runtime binaries for rdx-tools")
    parser.add_argument("--source", dest="renderdoc_source", default="", help="Repo-relative staging directory under the rdx-tools root")
    parser.add_argument("--python-home", default="", help="Absolute or repo-relative CPython home used to build the bundled runtime")
    parser.add_argument("--site-packages-source", default=".venv/Lib/site-packages", help="Repo-relative site-packages source for the bundled runtime")
    parser.add_argument("--python-version", default="", help="Override the bundled CPython version metadata")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    tools_root_path = _tools_root()
    out_root = tools_root_path / "binaries" / "windows" / "x64"
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "pymodules").mkdir(parents=True, exist_ok=True)

    bundled_python: dict[str, str] = {}

    renderdoc_source_raw = str(args.renderdoc_source or "").strip()
    if renderdoc_source_raw:
        src = _resolve_input_path(tools_root_path, renderdoc_source_raw, label="runtime source")
        if not src.is_dir():
            print(f"[pack] missing source directory: {src}")
            return 1

        for src_file in _iter_allowed_files(src):
            rel = src_file.relative_to(src)
            dst_file = out_root / rel
            if dst_file.suffix.lower() in DENY_SUFFIXES:
                continue
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src_file, dst_file)
            except PermissionError as exc:
                if dst_file.is_file():
                    print(f"[pack] locked target kept as-is: {dst_file} ({exc})")
                else:
                    print(f"[pack] copy failed: {src_file} -> {dst_file} ({exc})")
                    return 1

    python_home_raw = str(args.python_home or "").strip()
    if python_home_raw:
        try:
            bundled_python = _bundle_python_runtime(
                tools_root_path,
                out_root,
                python_home_raw,
                str(args.site_packages_source or "").strip() or None,
                str(args.python_version or "").strip() or None,
            )
        except RuntimeError as exc:
            print(f"[pack] {exc}")
            return 1

    manifest_entries = [
        _manifest_entry(path, out_root, worker_materialize=worker_materialize)
        for path, worker_materialize in _iter_manifest_files(out_root)
    ]

    if not manifest_entries:
        print("[pack] no runtime files found to package")
        return 1

    manifest = {
        "file_count": len(manifest_entries),
        "files": manifest_entries,
    }
    if bundled_python:
        manifest["bundled_python"] = bundled_python
    manifest_path = out_root / "manifest.runtime.json"
    write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"[pack] wrote {manifest_path} with {len(manifest_entries)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())