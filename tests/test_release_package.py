from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts import package_release, verify_release_package


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
        assert not any(name.startswith("rdx-tools/tests/") for name in names)
        assert "rdx-tools/intermediate/logs/secret.log" not in names
        manifest = json.loads(zf.read("rdx-tools/RELEASE_MANIFEST.json").decode("utf-8"))
    assert manifest["platform"] == "windows-x64"
    assert manifest["public_commands"] == ["rdx"]
    assert sorted(manifest["entrypoints"]) == ["bin/rdx", "cli/run_cli.py", "rdx.bat"]
    manifest_paths = {entry["path"] for entry in manifest["files"]}
    assert "pyproject.toml" in manifest_paths
    assert "uv.lock" not in manifest_paths
    assert not any(path == "tests" or path.startswith("tests/") for path in manifest_paths)


def test_verify_release_package_accepts_manifest_public_command_split(tmp_path: Path) -> None:
    root = tmp_path / "rdx-tools"
    for rel in ("rdx.bat", "bin/rdx", "cli/run_cli.py"):
        _write(root / rel)
    manifest = {
        "name": "rdx-tools",
        "version": "1.0.0",
        "platform": "windows-x64",
        "public_commands": ["rdx"],
        "entrypoints": ["rdx.bat", "bin/rdx", "cli/run_cli.py"],
        "files": [
            {"path": "rdx.bat", "size": 3, "sha256": "0" * 64},
            {"path": "bin/rdx", "size": 3, "sha256": "0" * 64},
            {"path": "cli/run_cli.py", "size": 3, "sha256": "0" * 64},
        ],
    }
    _write(root / "RELEASE_MANIFEST.json", json.dumps(manifest))

    verify_release_package._verify_release_manifest(root)


def test_verify_release_package_rejects_manifest_without_public_command(tmp_path: Path) -> None:
    root = tmp_path / "rdx-tools"
    for rel in ("rdx.bat", "bin/rdx", "cli/run_cli.py"):
        _write(root / rel)
    manifest = {
        "name": "rdx-tools",
        "version": "1.0.0",
        "platform": "windows-x64",
        "entrypoints": ["rdx.bat", "bin/rdx", "cli/run_cli.py"],
        "files": [
            {"path": "rdx.bat", "size": 3, "sha256": "0" * 64},
            {"path": "bin/rdx", "size": 3, "sha256": "0" * 64},
            {"path": "cli/run_cli.py", "size": 3, "sha256": "0" * 64},
        ],
    }
    _write(root / "RELEASE_MANIFEST.json", json.dumps(manifest))

    with pytest.raises(RuntimeError, match="public_commands"):
        verify_release_package._verify_release_manifest(root)


def test_verify_release_package_rejects_pre_ga_payload_path(tmp_path: Path) -> None:
    package = tmp_path / "rdx-tools-1.0.0-windows-x64.zip"
    pre_ga_path = "rdx-tools/rdx/" + "runtime_" + "materializer.py"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(pre_ga_path, "pre-ga\n")

    with pytest.raises(RuntimeError, match="pre-GA path"):
        verify_release_package._verify_no_pre_ga_payload(package)


def test_verify_release_package_rejects_pre_ga_payload_marker(tmp_path: Path) -> None:
    package = tmp_path / "rdx-tools-1.0.0-windows-x64.zip"
    pre_ga_marker = "worker_" + "materialize"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("rdx-tools/binaries/windows/x64/manifest.runtime.json", '{"flag":"' + pre_ga_marker + '"}')

    with pytest.raises(RuntimeError, match="pre-GA marker"):
        verify_release_package._verify_no_pre_ga_payload(package)
