from __future__ import annotations

import json
import zipfile
from pathlib import Path

from scripts import package_release


def _write(path: Path, content: str = "ok\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_package_release_builds_self_contained_zip(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "tools"
    for rel in (
        "AGENTS.md",
        "CHANGELOG.md",
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "rdx.bat",
        "bin/rdx",
        "cli/run_cli.py",
        "docs/quickstart.md",
        "policy/README.md",
        "rdx/__init__.py",
        "scripts/rdx_install.ps1",
        "scripts/smoke_cli.sh",
        "scripts/verify_release_package.py",
        "spec/tool_catalog.json",
        "binaries/windows/x64/manifest.runtime.json",
        "tests/fixtures/README.md",
    ):
        _write(root / rel)
    _write(root / "intermediate/logs/secret.log", "must not ship\n")

    monkeypatch.setattr(package_release, "_tools_root", lambda: root)

    rc = package_release.main(["--out-dir", "dist"])

    assert rc == 0
    package = next((root / "dist").glob("rdx-tools-*-windows-x64.zip"))
    assert (root / "dist" / "SHA256SUMS").is_file()
    with zipfile.ZipFile(package) as zf:
        names = set(zf.namelist())
        assert "rdx-tools/RELEASE_MANIFEST.json" in names
        assert "rdx-tools/SBOM.json" in names
        assert "rdx-tools/LICENSE_INVENTORY.json" in names
        assert "rdx-tools/pyproject.toml" in names
        assert "rdx-tools/scripts/rdx_install.ps1" in names
        assert "rdx-tools/uv.lock" not in names
        assert not any(name.startswith("rdx-tools/mcp/") for name in names)
        assert "rdx-tools/intermediate/logs/secret.log" not in names
        manifest = json.loads(zf.read("rdx-tools/RELEASE_MANIFEST.json").decode("utf-8"))
    assert manifest["platform"] == "windows-x64"
    assert "rdx.bat" in manifest["entrypoints"]
    manifest_paths = {entry["path"] for entry in manifest["files"]}
    assert "pyproject.toml" in manifest_paths
    assert "uv.lock" not in manifest_paths
    assert not any(path == "mcp" or path.startswith("mcp/") for path in manifest_paths)
