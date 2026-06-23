#!/usr/bin/env python3
"""Build a self-contained Windows x64 release zip for rdx-tools."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from rdx import __version__ as TOOL_VERSION
from scripts._shared import tools_root, write_text


PACKAGE_PLATFORM = "windows-x64"
PACKAGE_PREFIX = "rdx-tools"
EXCLUDE_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "intermediate",
}
EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo", ".pdb", ".ilk", ".exp", ".lib", ".rdc"}
RELEASE_ROOT_FILES = {
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "CHANGELOG.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "README.md",
    "pyproject.toml",
    "rdx.bat",
}
RELEASE_DIRS = {
    "bin",
    "binaries",
    "cli",
    "docs",
    "policy",
    "rdx",
    "scripts",
    "spec",
}


def _tools_root() -> Path:
    return tools_root(__file__)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _should_skip(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if path.is_file() and path.suffix.lower() in EXCLUDE_FILE_SUFFIXES:
        return True
    if rel.parts and rel.parts[0] == "tests":
        return True
    return False


def _copy_release_tree(root: Path, staging_root: Path) -> list[dict[str, object]]:
    copied: list[dict[str, object]] = []
    allowed_roots = RELEASE_ROOT_FILES | RELEASE_DIRS
    for path in sorted(root.rglob("*")):
        if path.is_dir() or _should_skip(path, root):
            continue
        rel = path.relative_to(root)
        if not rel.parts or rel.parts[0] not in allowed_roots:
            continue
        target = staging_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(
            {
                "path": rel.as_posix(),
                "size": int(target.stat().st_size),
                "sha256": _sha256(target),
            }
        )
    return copied


def _license_inventory(staging_root: Path) -> list[dict[str, str]]:
    rows = [{"name": "rdx-tools", "version": TOOL_VERSION, "license": "Apache-2.0", "path": "LICENSE"}]
    site_packages = staging_root / "binaries" / "windows" / "x64" / "python" / "Lib" / "site-packages"
    if not site_packages.is_dir():
        return rows
    for meta in sorted(site_packages.glob("*.dist-info/METADATA")):
        name = ""
        version = ""
        license_name = ""
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name: "):
                name = line[6:].strip()
            elif line.startswith("Version: "):
                version = line[9:].strip()
            elif line.startswith("License: "):
                license_name = line[9:].strip()
        if name:
            rows.append(
                {
                    "name": name,
                    "version": version,
                    "license": license_name or "UNKNOWN",
                    "path": meta.relative_to(staging_root).as_posix(),
                }
            )
    return rows


def _write_release_metadata(staging_root: Path, files: list[dict[str, object]]) -> None:
    manifest = {
        "name": PACKAGE_PREFIX,
        "version": TOOL_VERSION,
        "platform": PACKAGE_PLATFORM,
        "public_commands": ["rdx"],
        "entrypoints": ["rdx.bat", "bin/rdx", "cli/run_cli.py"],
        "file_count": len(files),
        "files": files,
    }
    licenses = _license_inventory(staging_root)
    sbom = {
        "schema": "rdx-tools.sbom.v1",
        "name": PACKAGE_PREFIX,
        "version": TOOL_VERSION,
        "platform": PACKAGE_PLATFORM,
        "components": licenses,
    }
    write_text(staging_root / "RELEASE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    write_text(staging_root / "LICENSE_INVENTORY.json", json.dumps(licenses, ensure_ascii=False, indent=2) + "\n")
    write_text(staging_root / "SBOM.json", json.dumps(sbom, ensure_ascii=False, indent=2) + "\n")


def _zip_dir(staging_root: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(staging_root.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=f"{PACKAGE_PREFIX}/{path.relative_to(staging_root).as_posix()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build rdx-tools release package")
    parser.add_argument("--out-dir", default="dist", help="Output directory under the rdx-tools root")
    parser.add_argument("--version", default=TOOL_VERSION, help="Expected package version")
    args = parser.parse_args(argv)

    if str(args.version) != TOOL_VERSION:
        print(f"[release] version mismatch: requested={args.version} package={TOOL_VERSION}")
        return 2

    root = _tools_root().resolve()
    out_dir = (root / str(args.out_dir)).resolve()
    staging_parent = root / "intermediate" / "release"
    staging_root = staging_parent / PACKAGE_PREFIX
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = _copy_release_tree(root, staging_root)
    _write_release_metadata(staging_root, files)

    package_name = f"{PACKAGE_PREFIX}-{TOOL_VERSION}-{PACKAGE_PLATFORM}.zip"
    package_path = out_dir / package_name
    _zip_dir(staging_root, package_path)

    sha = _sha256(package_path)
    checksum_path = out_dir / "SHA256SUMS"
    write_text(checksum_path, f"{sha}  {package_name}\n")
    report = [
        "# rdx-tools Release Report",
        "",
        f"- version: {TOOL_VERSION}",
        f"- platform: {PACKAGE_PLATFORM}",
        f"- package: {package_path}",
        f"- sha256: {sha}",
        f"- files: {len(files)}",
        "",
    ]
    write_text(out_dir / "release_report.md", "\n".join(report))
    print(f"[release] package: {package_path}")
    print(f"[release] sha256: {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
