from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.generate_tool_reference import generate_tool_reference

ROOT = Path(__file__).resolve().parents[1]


def test_tool_reference_is_generated_from_catalog() -> None:
    catalog_path = ROOT / "spec" / "tool_catalog.json"
    doc_path = ROOT / "docs" / "tool-reference.md"
    payload = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    tools = payload["tools"]
    groups = {str(tool.get("group") or "") for tool in tools}

    assert doc_path.read_text(encoding="utf-8-sig") == generate_tool_reference(catalog_path)
    assert f"- Tool count: {len(tools)}" in doc_path.read_text(encoding="utf-8-sig")
    assert f"- Group count: {len(groups)}" in doc_path.read_text(encoding="utf-8-sig")
    for tool in tools:
        assert str(tool["name"]) in doc_path.read_text(encoding="utf-8-sig")


def test_rdx_native_playbook_has_fixed_recipe_set_and_required_sop_topics() -> None:
    text = (ROOT / "docs" / "rdx-native-agent-playbook.md").read_text(encoding="utf-8-sig")
    recipes = [line for line in text.splitlines() if line.startswith("### ")]

    assert recipes == [
        "### 1. Open Capture",
        "### 2. Unknown Frame Triage",
        "### 3. Pipeline And Resource Inspect",
        "### 4. Visual Export And Preview",
        "### 5. Pixel Debug",
        "### 6. Shader Edit With edit_plan",
        "### 7. Android Remote Open And Replay",
        "### 8. Bug Report Pack",
    ]
    required_terms = [
        "rdx --json doctor",
        "--daemon-context",
        "rdx context status --json",
        "rdx context update",
        "rdx context clear",
        "VFS first",
        "preview.display",
        "rd.remote.connect",
        "rd.remote.ping",
        "rd.capture.open_replay",
        "edit_plan",
        "Output Size Management",
        "Failure Recovery",
    ]
    for term in required_terms:
        assert term in text


def test_public_rdc_fixtures_have_expected_size_and_hash() -> None:
    expected = {
        "hello_triangle.rdc": (75478, "00797a27e6316a0cf4369327f9db30a21635fa757673b3f9712af07989145ba8"),
        "vkcube.rdc": (75478, "00797a27e6316a0cf4369327f9db30a21635fa757673b3f9712af07989145ba8"),
        "vkcube_validation.rdc": (65913, "c50cd1e7c29241c64fd33faf07cb35e802f9dc85692a8512aa36db01c956b385"),
    }
    for name, (size, sha256) in expected.items():
        path = ROOT / "tests" / "fixtures" / name
        assert path.is_file()
        assert path.stat().st_size == size
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha256


def test_fixture_policy_and_third_party_notices_cover_copied_assets() -> None:
    fixture_readme = (ROOT / "tests" / "fixtures" / "README.md").read_text(encoding="utf-8-sig")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8-sig")

    for name in ("hello_triangle.rdc", "vkcube.rdc", "vkcube_validation.rdc"):
        assert name in fixture_readme
        assert name in notices
    assert "MIT" in notices
    assert "BANANASJIM" in notices
    assert "Release behavior: excluded" in notices


def test_project_license_metadata_is_apache_2() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = { text = "Apache-2.0" }' in pyproject
    assert "Apache License" in (ROOT / "LICENSE").read_text(encoding="utf-8", errors="replace")
