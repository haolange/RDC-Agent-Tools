from __future__ import annotations

from pathlib import Path

from scripts import release_gate


def _prepare_root(root: Path) -> None:
    for rel in release_gate.REQUIRED_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel in release_gate.REQUIRED_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")


def _write_report(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok\n", encoding="utf-8")


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
    sample.write_text("mentions extensions/legacy path\n", encoding="utf-8")

    def _missing_rg(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("rg missing")

    monkeypatch.setattr(release_gate.subprocess, "run", _missing_rg)

    ok, detail = release_gate._rg_no_match(release_gate.ScanRule("extensions/", literal=True), tmp_path)

    assert not ok
    assert "python fallback" in detail
    assert "extensions/legacy path" in detail


def test_release_gate_accepts_current_reports(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    for rel in release_gate.CURRENT_REPORTS:
        _write_report(tmp_path, rel)

    monkeypatch.setattr(release_gate, "_tools_root", lambda: tmp_path)
    monkeypatch.setattr(release_gate, "_run", lambda cmd, cwd: (True, "ok"))
    monkeypatch.setattr(release_gate, "_check_manifest", lambda root: (True, "manifest ok"))
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md"])

    assert rc == 0
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "PASS `reports:smoke-suite`" in report
    assert "using current smoke reports" in report


def test_release_gate_accepts_missing_reports_in_clean_checkout(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)

    monkeypatch.setattr(release_gate, "_tools_root", lambda: tmp_path)
    monkeypatch.setattr(release_gate, "_run", lambda cmd, cwd: (True, "ok"))
    monkeypatch.setattr(release_gate, "_check_manifest", lambda root: (True, "manifest ok"))
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md"])

    assert rc == 0
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "PASS `reports:smoke-suite`" in report
    assert "smoke reports optional in clean checkout" in report


def test_release_gate_rejects_incomplete_current_reports(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    for rel in release_gate.CURRENT_REPORTS[:-1]:
        _write_report(tmp_path, rel)

    monkeypatch.setattr(release_gate, "_tools_root", lambda: tmp_path)
    monkeypatch.setattr(release_gate, "_run", lambda cmd, cwd: (True, "ok"))
    monkeypatch.setattr(release_gate, "_check_manifest", lambda root: (True, "manifest ok"))
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md"])

    assert rc == 1
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "FAIL `reports:smoke-suite`" in report
    assert "incomplete smoke reports:" in report
    assert release_gate.CURRENT_REPORTS[-1] in report


def test_release_gate_requires_reports_when_fixture_exists(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    fixture = tmp_path / "tests" / "fixtures" / "sample.rdc"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("fixture\n", encoding="utf-8")

    monkeypatch.setattr(release_gate, "_tools_root", lambda: tmp_path)
    monkeypatch.setattr(release_gate, "_run", lambda cmd, cwd: (True, "ok"))
    monkeypatch.setattr(release_gate, "_check_manifest", lambda root: (True, "manifest ok"))
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md"])

    assert rc == 1
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "FAIL `reports:smoke-suite`" in report
    assert "missing current reports:" in report


def test_release_gate_requires_reports_when_flagged(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)

    monkeypatch.setattr(release_gate, "_tools_root", lambda: tmp_path)
    monkeypatch.setattr(release_gate, "_run", lambda cmd, cwd: (True, "ok"))
    monkeypatch.setattr(release_gate, "_check_manifest", lambda root: (True, "manifest ok"))
    monkeypatch.setattr(release_gate, "_rg_no_match", lambda pattern, cwd: (True, ""))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md", "--require-smoke-reports"])

    assert rc == 1
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "FAIL `reports:smoke-suite`" in report
    assert "missing current reports:" in report


def test_release_gate_main_survives_rg_permission_error(monkeypatch, tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    for rel in release_gate.CURRENT_REPORTS:
        _write_report(tmp_path, rel)
    bad_ref = tmp_path / "docs" / "sample.md"
    bad_ref.parent.mkdir(parents=True, exist_ok=True)
    bad_ref.write_text("legacy extensions/path reference\n", encoding="utf-8")

    def _deny_rg(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PermissionError("access denied")

    monkeypatch.setattr(release_gate.subprocess, "run", _deny_rg)
    monkeypatch.setattr(release_gate, "_tools_root", lambda: tmp_path)
    monkeypatch.setattr(release_gate, "_run", lambda cmd, cwd: (True, "ok"))
    monkeypatch.setattr(release_gate, "_check_manifest", lambda root: (True, "manifest ok"))

    rc = release_gate.main(["--report", "intermediate/logs/release_gate_report.md"])

    assert rc == 1
    report = (tmp_path / "intermediate" / "logs" / "release_gate_report.md").read_text(encoding="utf-8")
    assert "FAIL `refs:no_extensions_path`" in report
    assert "python fallback after PermissionError" in report
