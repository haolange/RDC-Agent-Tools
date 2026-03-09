from __future__ import annotations

from pathlib import Path

import pytest

from scripts import tool_contract_check


def test_classify_result_uses_structured_capability_details() -> None:
    payload = {
        "schema_version": "2.0.0",
        "tool_version": "1.0.0",
        "result_kind": "rd.shader.extract_binary",
        "ok": False,
        "data": {},
        "artifacts": [],
        "error": {
            "code": "shader_binary_export_unavailable",
            "category": "capability",
            "message": "Shader binary extraction is not available via this replay backend",
            "details": {
                "capability": "shader_binary_export",
                "optional": True,
                "source": "runtime_build",
            },
        },
    }

    status, reason, error_code, issue_type, fix_hint, impact_scope = tool_contract_check._classify_result(
        "rd.shader.extract_binary",
        payload,
        "",
        "",
        contract_ok=True,
        matrix="local",
        transport="mcp",
        tool_error="",
    )

    assert status == "scope_skip"
    assert error_code == "shader_binary_export_unavailable"
    assert issue_type == "capability_boundary"
    assert "shader binary export path" in impact_scope
    assert "scope_skip" in status
    assert reason == "Shader binary extraction is not available via this replay backend"
    assert "backend gaps" in fix_hint


def test_build_args_uses_debug_pc_for_run_to_and_breakpoints(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="local", rdc_path=tmp_path / "sample.rdc")
    state.session_id = "sess_demo"
    state.shader_debug_id = "sdbg_demo"
    state.debug_pc = 12

    files = {
        "artifacts": tmp_path / "artifacts",
        "sample": tmp_path / "sample.bin",
        "text_a": tmp_path / "a.txt",
        "text_b": tmp_path / "b.txt",
        "png_a": tmp_path / "a.png",
        "png_b": tmp_path / "b.png",
        "zip_out": tmp_path / "bundle.zip",
    }

    run_to_args = tool_contract_check._build_args(
        "rd.debug.run_to",
        ["session_id", "shader_debug_id", "target", "timeout_ms"],
        state,
        files,
    )
    assert run_to_args["target"] == {"pc": 12}
    assert run_to_args["timeout_ms"] == 50

    breakpoint_args = tool_contract_check._build_args(
        "rd.debug.set_breakpoints",
        ["session_id", "shader_debug_id", "breakpoints"],
        state,
        files,
    )
    assert breakpoint_args["breakpoints"] == [{"pc": 12}]


def test_parse_args_requires_local_rdc(monkeypatch, tmp_path: Path) -> None:
    out_json = tmp_path / "tool_contract_report.json"
    out_md = tmp_path / "tool_contract_report.md"
    monkeypatch.setattr(
        tool_contract_check.sys,
        "argv",
        [
            "tool_contract_check.py",
            "--transport",
            "mcp",
            "--out-json",
            str(out_json),
            "--out-md",
            str(out_md),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        tool_contract_check._parse_args()

    assert exc.value.code == 2


def test_parse_args_requires_remote_rdc_for_both_transport(monkeypatch, tmp_path: Path) -> None:
    local_rdc = tmp_path / "local.rdc"
    local_rdc.write_text("sample", encoding="utf-8")
    out_json = tmp_path / "tool_contract_report.json"
    out_md = tmp_path / "tool_contract_report.md"
    monkeypatch.setattr(
        tool_contract_check.sys,
        "argv",
        [
            "tool_contract_check.py",
            "--local-rdc",
            str(local_rdc),
            "--transport",
            "both",
            "--out-json",
            str(out_json),
            "--out-md",
            str(out_md),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        tool_contract_check._parse_args()

    assert exc.value.code == 2


def test_build_args_for_vfs_tools_use_vfs_paths(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="local", rdc_path=tmp_path / "sample.rdc", session_id="sess_demo")
    files = {
        "artifacts": tmp_path / "artifacts",
        "sample": tmp_path / "sample.bin",
        "text_a": tmp_path / "a.txt",
        "text_b": tmp_path / "b.txt",
        "png_a": tmp_path / "a.png",
        "png_b": tmp_path / "b.png",
        "zip_out": tmp_path / "bundle.zip",
    }

    tree_args = tool_contract_check._build_args("rd.vfs.tree", ["path", "session_id", "depth"], state, files)
    resolve_args = tool_contract_check._build_args("rd.vfs.resolve", ["path", "session_id"], state, files)

    assert tree_args == {"path": "/draws", "session_id": "sess_demo", "depth": 2}
    assert resolve_args == {"path": "/pipeline", "session_id": "sess_demo"}
