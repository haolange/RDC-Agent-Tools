from __future__ import annotations

import json
import zipfile
from pathlib import Path

from scripts import release_gate


def _prepare_root(root: Path) -> None:
    for rel in release_gate.REQUIRED_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel in release_gate.REQUIRED_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")


def _write_smoke_log(root: Path, *, passed: bool = True) -> None:
    path = root / release_gate.BASH_SMOKE_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = "[smoke] PASS: CLI smoke completed" if passed else "[smoke] FAIL: capture open"
    path.write_text(marker + "\n", encoding="utf-8")


def _mock_release_gate_basics(monkeypatch, root: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(release_gate, "_tools_root", lambda: root)
    monkeypatch.setattr(release_gate, "_run", lambda cmd, cwd, **kwargs: (True, "ok"))
    monkeypatch.setattr(release_gate, "_run_public_command", lambda args, cwd: (True, "usage: rdx"))
    monkeypatch.setattr(release_gate, "_run_windows_launcher_file", lambda args, cwd: (True, "ok"))
    monkeypatch.setattr(
        release_gate,
        "_run_public_command_expect_error",
        lambda args, cwd, expected_codes: (True, next(iter(expected_codes))),
    )
    monkeypatch.setattr(release_gate, "_check_manifest", lambda root: (True, "manifest ok"))
    monkeypatch.setattr(release_gate, "_check_bundled_python", lambda: (True, "bundled python ok"))
    monkeypatch.setattr(
        release_gate,
        "_check_user_docs_no_python_bootstrap",
        lambda root: (True, "user docs ok"),
    )
    monkeypatch.setattr(
        release_gate,
        "_check_user_docs_no_bat_command_examples",
        lambda root: (True, "user docs command ok"),
    )
    monkeypatch.setattr(release_gate, "_check_catalog_public_surface", lambda root: (True, "catalog ok"))
    monkeypatch.setattr(release_gate, "_check_no_mcp_public_surface", lambda root: (True, "mcp ok"))


def test_rg_no_match_falls_back_when_rg_missing(monkeypatch, tmp_path: Path) -> None:
    sample = tmp_path / "docs" / "sample.md"
    sample.parent.mkdir(parents=True, exist_ok=True)
    token = "frame" + "works"
    sample.write_text(f"contains {token} token\n", encoding="utf-8")

    def _missing_rg(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("rg missing")

    monkeypatch.setattr(release_gate.subprocess, "run", _missing_rg)

    ok, detail = release_gate._rg_no_match(release_gate.ScanRule("frame" + "works"), tmp_path)

    assert not ok
    assert "python fallback" in detail
    assert "sample.md" in detail


def test_rg_no_match_falls_back_when_rg_permission_denied(monkeypatch, tmp_path: Path) -> None:
    sample = tmp_path / "docs" / "sample.md"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text("clean content\n", encoding="utf-8")

    def _deny_rg(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PermissionError("access denied")

    monkeypatch.setattr(release_gate.subprocess, "run", _deny_rg)

    ok, detail = release_gate._rg_no_match(release_gate.ScanRule("frame" + "works"), tmp_path)

    assert ok
    assert detail == ""


def test_rg_no_match_literal_falls_back_when_rg_missing(monkeypatch, tmp_path: Path) -> None:
    sample = tmp_path / "docs" / "sample.md"
    sample.parent.mkdir(parents=True, exist_ok=True)
    pre_ga_token = "ext" + "ensions/"
    sample.write_text(f"mentions {pre_ga_token}pre-GA path\n", encoding="utf-8")

    def _missing_rg(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("rg missing")

    monkeypatch.setattr(release_gate.subprocess, "run", _missing_rg)

    ok, detail = release_gate._rg_no_match(release_gate.ScanRule(pre_ga_token, literal=True), tmp_path)

    assert not ok
    assert "python fallback" in detail
    assert f"{pre_ga_token}pre-GA path" in detail


def test_release_gate_accepts_current_bash_smoke_log(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    _write_smoke_log(tmp_path)

    _mock_release_gate_basics(monkeypatch, tmp_path)
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md"])

    assert rc == 0
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "PASS `reports:smoke-suite`" in report
    assert "bash CLI smoke log is present and passed" in report


def test_release_gate_accepts_missing_reports_in_clean_checkout(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)

    _mock_release_gate_basics(monkeypatch, tmp_path)
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md"])

    assert rc == 0
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "PASS `reports:smoke-suite`" in report
    assert "bash CLI smoke optional" in report


def test_release_gate_rejects_failing_bash_smoke_log_when_flagged(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    _write_smoke_log(tmp_path, passed=False)

    _mock_release_gate_basics(monkeypatch, tmp_path)
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md", "--require-smoke-reports"])

    assert rc == 1
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "FAIL `reports:smoke-suite`" in report
    assert "bash CLI smoke log did not contain [smoke] PASS" in report


def test_release_gate_does_not_special_case_fixture_directory(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    fixture = tmp_path / "tests" / "fixtures" / "sample.rdc"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("fixture\n", encoding="utf-8")

    _mock_release_gate_basics(monkeypatch, tmp_path)
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md"])

    assert rc == 0
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "PASS `reports:smoke-suite`" in report
    assert "bash CLI smoke optional" in report


def test_release_gate_requires_reports_when_flagged(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)

    _mock_release_gate_basics(monkeypatch, tmp_path)
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md", "--require-smoke-reports"])

    assert rc == 1
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "FAIL `reports:smoke-suite`" in report
    assert "missing bash CLI smoke log" in report


def test_release_gate_main_survives_rg_permission_error(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    _write_smoke_log(tmp_path)
    bad_ref = tmp_path / "docs" / "sample.md"
    bad_ref.parent.mkdir(parents=True, exist_ok=True)
    pre_ga_token = "ext" + "ensions/"
    bad_ref.write_text(f"pre-GA {pre_ga_token}path reference\n", encoding="utf-8")

    def _deny_rg(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PermissionError("access denied")

    monkeypatch.setattr(release_gate.subprocess, "run", _deny_rg)
    _mock_release_gate_basics(monkeypatch, tmp_path)

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md"])

    assert rc == 1
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "FAIL `refs:no_extensions_path`" in report
    assert "python fallback after PermissionError" in report


def test_release_gate_requires_passing_bash_smoke_log_when_flagged(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    _write_smoke_log(tmp_path, passed=False)

    _mock_release_gate_basics(monkeypatch, tmp_path)
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md", "--require-smoke-reports"])

    assert rc == 1
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "FAIL `reports:smoke-suite`" in report
    assert "bash CLI smoke log did not contain [smoke] PASS" in report


def test_release_gate_accepts_passing_bash_smoke_log_when_flagged(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    _write_smoke_log(tmp_path)

    _mock_release_gate_basics(monkeypatch, tmp_path)
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md", "--require-smoke-reports"])

    assert rc == 0
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "PASS `reports:smoke-suite`" in report
    assert "bash CLI smoke log is present and passed" in report


def test_release_gate_requires_release_package_when_flagged(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    _write_smoke_log(tmp_path)

    _mock_release_gate_basics(monkeypatch, tmp_path)
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md", "--require-release-package"])

    assert rc == 1
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "FAIL `release:package`" in report
    assert "missing release package" in report


def test_release_gate_verifies_release_package_when_present(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    _write_smoke_log(tmp_path)
    package = tmp_path / "dist" / "rdx-tools-1.0.0-windows-x64.zip"
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(b"zip")
    (package.parent / "SHA256SUMS").write_text("abc  rdx-tools-1.0.0-windows-x64.zip\n", encoding="utf-8")

    _mock_release_gate_basics(monkeypatch, tmp_path)
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))
    monkeypatch.setattr(
        release_gate,
        "_check_package_matches_source",
        lambda root, package_path: (True, "source manifest matched 1 files"),
    )

    rc = release_gate.main(
        [
            "--report",
            "intermediate/logs/release_gate_report.md",
            "--require-release-package",
            "--release-package",
            str(package),
        ],
    )

    assert rc == 0
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "PASS `release:package`" in report


def test_release_gate_rejects_stale_release_package_manifest(tmp_path: Path) -> None:
    source = tmp_path / "rdx.bat"
    source.write_text("new\n", encoding="utf-8")
    package = tmp_path / "dist" / "rdx-tools-1.0.0-windows-x64.zip"
    package.parent.mkdir(parents=True, exist_ok=True)
    stale_manifest = {
        "name": "rdx-tools",
        "version": "1.0.0",
        "platform": "windows-x64",
        "public_commands": ["rdx"],
        "entrypoints": ["rdx.bat"],
        "file_count": 1,
        "files": [
            {
                "path": "rdx.bat",
                "size": 4,
                "sha256": "0" * 64,
            }
        ],
    }
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("rdx-tools/RELEASE_MANIFEST.json", json.dumps(stale_manifest))

    ok, detail = release_gate._check_package_matches_source(tmp_path, package)

    assert not ok
    assert "stale relative to source tree" in detail


def test_release_gate_accepts_zero_byte_files_in_release_manifest(tmp_path: Path) -> None:
    source = tmp_path / "rdx.bat"
    source.write_bytes(b"")
    package = tmp_path / "dist" / "rdx-tools-1.0.0-windows-x64.zip"
    package.parent.mkdir(parents=True, exist_ok=True)
    source_sha = release_gate._sha256(source)
    manifest = {
        "name": "rdx-tools",
        "version": "1.0.0",
        "platform": "windows-x64",
        "public_commands": ["rdx"],
        "entrypoints": ["rdx.bat"],
        "file_count": 1,
        "files": [{"path": "rdx.bat", "size": 0, "sha256": source_sha}],
    }
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("rdx-tools/RELEASE_MANIFEST.json", json.dumps(manifest))

    ok, detail = release_gate._check_package_matches_source(tmp_path, package)

    assert ok
    assert "source manifest matched 1 files" in detail
