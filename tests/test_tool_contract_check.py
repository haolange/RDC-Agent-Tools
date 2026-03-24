from __future__ import annotations

from pathlib import Path

import pytest

from scripts import tool_contract_check


def test_classify_result_uses_structured_capability_details() -> None:
    payload = {
        "schema_version": "3.0.0",
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
        "meta": {},
        "projections": {},
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


def test_build_args_for_export_texture_uses_png_output_path(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="local", rdc_path=tmp_path / "sample.rdc")
    files = {
        "artifacts": tmp_path / "artifacts",
        "sample": tmp_path / "sample.bin",
        "text_a": tmp_path / "a.txt",
        "text_b": tmp_path / "b.txt",
        "png_a": tmp_path / "a.png",
        "png_b": tmp_path / "b.png",
        "zip_out": tmp_path / "bundle.zip",
    }

    args = tool_contract_check._build_args(
        "rd.export.texture",
        ["session_id", "texture_id", "output_path"],
        state,
        files,
    )

    assert str(args["output_path"]).endswith("_texture_out.png")


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


def test_build_args_for_session_update_context_uses_notes_round_trip_payload(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="local", rdc_path=tmp_path / "sample.rdc")
    files = {
        "artifacts": tmp_path / "artifacts",
        "sample": tmp_path / "sample.bin",
        "text_a": tmp_path / "a.txt",
        "text_b": tmp_path / "b.txt",
        "png_a": tmp_path / "a.png",
        "png_b": tmp_path / "b.png",
        "zip_out": tmp_path / "bundle.zip",
    }

    args = tool_contract_check._build_args(
        "rd.session.update_context",
        ["key", "value"],
        state,
        files,
    )

    assert args == {"key": "notes", "value": "local contract test"}


def test_remote_connect_args_default_to_android_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RDX_REMOTE_CONNECT_TRANSPORT", raising=False)
    monkeypatch.delenv("RDX_REMOTE_DEVICE_SERIAL", raising=False)

    args = tool_contract_check._remote_connect_args()

    assert args["host"] == "127.0.0.1"
    assert args["port"] == 38920
    assert args["options"]["transport"] == "adb_android"


def test_remote_connect_args_allow_renderdoc_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RDX_REMOTE_CONNECT_TRANSPORT", "renderdoc")
    monkeypatch.delenv("RDX_REMOTE_DEVICE_SERIAL", raising=False)

    args = tool_contract_check._remote_connect_args()

    assert "options" not in args


def test_build_args_for_remote_connect_uses_android_bootstrap_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RDX_REMOTE_CONNECT_TRANSPORT", raising=False)
    monkeypatch.delenv("RDX_REMOTE_DEVICE_SERIAL", raising=False)
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")
    files = {
        "artifacts": tmp_path / "artifacts",
        "sample": tmp_path / "sample.bin",
        "text_a": tmp_path / "a.txt",
        "text_b": tmp_path / "b.txt",
        "png_a": tmp_path / "a.png",
        "png_b": tmp_path / "b.png",
        "zip_out": tmp_path / "bundle.zip",
    }

    args = tool_contract_check._build_args("rd.remote.connect", ["host", "port", "timeout_ms"], state, files)

    assert args["host"] == "127.0.0.1"
    assert args["port"] == 38920
    assert args["options"]["transport"] == "adb_android"


def test_sample_compatibility_detects_cross_gpu_replay_error() -> None:
    message = (
        "OpenCapture failed with status: Current replaying hardware unsupported or incompatible with captured hardware: "
        "Capture requires extension 'VK_EXT_fragment_density_map' which is not supported\n\n"
        "Capture was made on: Qualcomm Adreno (TM) 650, 512.502.0\n"
        "Replayed on: nVidia NVIDIA GeForce RTX 4070 SUPER, 576.2.0\n"
        "Captures are not commonly portable between GPUs from different vendors."
    )

    assert tool_contract_check._is_sample_compatibility_error(message, "internal_error") is True


def test_track_tool_side_effects_updates_remote_handle_state(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")

    tool_contract_check._track_tool_side_effects(
        "rd.remote.connect",
        {},
        {
            "ok": True,
            "data": {"remote_id": "remote_demo"},
        },
        state,
    )
    assert state.remote_id == "remote_demo"

    tool_contract_check._track_tool_side_effects(
        "rd.remote.disconnect",
        {"remote_id": "remote_demo"},
        {
            "ok": True,
            "data": {"detail": {"connected": False}},
        },
        state,
    )
    assert state.remote_id is None
