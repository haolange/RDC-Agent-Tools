from __future__ import annotations

import asyncio
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


def test_build_args_for_remote_shader_compile_prefers_glsl(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc", session_id="sess_demo")
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
        "rd.shader.compile",
        ["session_id", "source", "source_encoding", "stage", "entry", "target", "defines", "include_dirs", "additional_args", "output_path"],
        state,
        files,
    )

    assert args["session_id"] == "sess_demo"
    assert args["source_encoding"] == "glsl"
    assert args["target"] == "spirv"
    assert "#version 450" in args["source"]


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

    assert tree_args == {"path": "/context", "session_id": "sess_demo", "depth": 1}
    assert resolve_args == {"path": "/context", "session_id": "sess_demo"}


def test_build_args_for_resource_contents_prefers_in_memory_artifact_flow(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc", session_id="sess_demo")
    state.resource_id = "ResourceId::tex"
    files = {
        "artifacts": tmp_path / "artifacts",
        "sample": tmp_path / "sample.bin",
        "text_a": tmp_path / "a.txt",
        "text_b": tmp_path / "b.txt",
        "png_a": tmp_path / "a.png",
        "png_b": tmp_path / "b.png",
        "zip_out": tmp_path / "bundle.zip",
    }

    initial_args = tool_contract_check._build_args("rd.resource.get_initial_contents", ["session_id", "resource_id", "output_path"], state, files)
    current_args = tool_contract_check._build_args("rd.resource.get_current_contents", ["session_id", "resource_id", "subresource", "range", "output_path"], state, files)

    assert initial_args == {"session_id": "sess_demo", "resource_id": "ResourceId::tex"}
    assert current_args == {
        "session_id": "sess_demo",
        "resource_id": "ResourceId::tex",
        "subresource": {"mip": 0, "slice": 0, "sample": 0},
        "range": {},
    }


def test_build_args_fall_back_to_known_handles_when_current_state_is_empty(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")
    state.known_session_ids = ["sess_old", "sess_live"]
    state.known_capture_file_ids = ["cap_old", "cap_live"]
    state.shader_id = "ResourceId::178867"
    state.texture_id = "ResourceId::178817"
    files = {
        "artifacts": tmp_path / "artifacts",
        "sample": tmp_path / "sample.bin",
        "text_a": tmp_path / "a.txt",
        "text_b": tmp_path / "b.txt",
        "png_a": tmp_path / "a.png",
        "png_b": tmp_path / "b.png",
        "zip_out": tmp_path / "bundle.zip",
    }

    open_replay_args = tool_contract_check._build_args("rd.capture.open_replay", ["capture_file_id", "options"], state, files)
    macro_args = tool_contract_check._build_args("rd.macro.shader_hotfix_validate", ["session_id", "replacement", "validation", "output_dir"], state, files)
    session_value = tool_contract_check._default_for_id("session_id", state)

    assert open_replay_args["capture_file_id"] == "cap_live"
    assert macro_args["session_id"] == "sess_live"
    assert session_value == "sess_live"


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


def test_build_args_for_remote_open_replay_uses_live_remote_handle(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")
    state.capture_file_id = "capf_remote"
    state.live_remote_id = "remote_live"
    files = {
        "artifacts": tmp_path / "artifacts",
        "sample": tmp_path / "sample.bin",
        "text_a": tmp_path / "a.txt",
        "text_b": tmp_path / "b.txt",
        "png_a": tmp_path / "a.png",
        "png_b": tmp_path / "b.png",
        "zip_out": tmp_path / "bundle.zip",
    }

    args = tool_contract_check._build_args("rd.capture.open_replay", ["capture_file_id", "options"], state, files)

    assert args == {"capture_file_id": "capf_remote", "options": {"remote_id": "remote_live"}}


def test_build_args_for_export_screenshot_includes_event_bound_target(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(
        matrix="local",
        rdc_path=tmp_path / "sample.rdc",
        session_id="sess_demo",
    )
    state.event_id = 6152
    state.texture_id = "ResourceId::178817"
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
        "rd.export.screenshot",
        ["session_id", "target", "event_id", "output_path", "file_format"],
        state,
        files,
    )

    assert args["session_id"] == "sess_demo"
    assert args["event_id"] == 6152
    assert args["target"] == {"texture_id": "ResourceId::178817"}
    assert str(args["output_path"]).endswith("local_rd_export_screenshot.out")
    assert args["file_format"] == "png"


def test_tool_matrix_promotes_session_bound_tools_in_remote_only_mode() -> None:
    assert tool_contract_check._tool_matrix("rd.event.get_actions", ["session_id"], remote_only=True) == "remote"
    assert tool_contract_check._tool_matrix("rd.session.get_context", [], remote_only=True) == "remote"
    assert tool_contract_check._tool_matrix("rd.util.diff_text", ["a", "b"], remote_only=True) == "local"


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
    assert state.live_remote_id == "remote_demo"

    tool_contract_check._track_tool_side_effects(
        "rd.remote.disconnect",
        {"remote_id": "remote_demo"},
        {
            "ok": True,
            "data": {"detail": {"connected": False}},
        },
        state,
    )
    assert state.live_remote_id is None


def test_track_tool_side_effects_updates_current_capture_handle(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")

    tool_contract_check._track_tool_side_effects(
        "rd.capture.open_file",
        {},
        {
            "ok": True,
            "data": {"capture_file_id": "capf_demo"},
        },
        state,
    )

    assert state.capture_file_id == "capf_demo"
    assert state.known_capture_file_ids == ["capf_demo"]


def test_track_tool_side_effects_updates_current_session_handle(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")

    tool_contract_check._track_tool_side_effects(
        "rd.capture.open_replay",
        {"options": {"remote_id": "remote_demo"}},
        {
            "ok": True,
            "data": {"session_id": "sess_demo"},
        },
        state,
    )

    assert state.session_id == "sess_demo"
    assert state.known_session_ids == ["sess_demo"]


def test_track_tool_side_effects_preserves_existing_focus_texture_and_buffer(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")
    state.texture_id = "ResourceId::focus"
    state.resource_id = "ResourceId::focus"
    state.buffer_id = "ResourceId::buffer-focus"

    tool_contract_check._track_tool_side_effects(
        "rd.resource.list_textures",
        {},
        {
            "ok": True,
            "data": {"textures": [{"texture_id": "ResourceId::other"}]},
        },
        state,
    )
    tool_contract_check._track_tool_side_effects(
        "rd.resource.list_buffers",
        {},
        {
            "ok": True,
            "data": {"buffers": [{"buffer_id": "ResourceId::buffer-other"}]},
        },
        state,
    )

    assert state.texture_id == "ResourceId::focus"
    assert state.resource_id == "ResourceId::focus"
    assert state.buffer_id == "ResourceId::buffer-focus"


def test_track_tool_side_effects_records_remote_targets_and_captures(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")

    tool_contract_check._track_tool_side_effects(
        "rd.remote.list_targets",
        {"remote_id": "remote_demo"},
        {
            "ok": True,
            "data": {"targets": [{"target_id": "17"}, {"target_id": "23"}]},
        },
        state,
    )
    tool_contract_check._track_tool_side_effects(
        "rd.remote.list_captures",
        {"remote_id": "remote_demo"},
        {
            "ok": True,
            "data": {"captures": [{"capture_id": "11", "target_id": "23"}]},
        },
        state,
    )

    assert state.target_id == "23"
    assert state.capture_id == "11"
    assert state.known_target_ids == ["17", "23"]
    assert state.known_capture_ids == ["11"]


def test_default_for_id_does_not_invent_dummy_runtime_handles(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")

    assert tool_contract_check._default_for_id("remote_id", state) is None
    assert tool_contract_check._default_for_id("target_id", state) is None
    assert tool_contract_check._default_for_id("capture_id", state) is None


def test_preflight_scope_skip_respects_forced_remote_skip_tools(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")

    skip = tool_contract_check._preflight_scope_skip("rd.remote.list_targets", ["remote_id"], state)

    assert skip is not None
    assert skip[1] == "remote_live_tool_skipped"
    assert skip[2] == "scope_skip"


def test_ensure_context_keeps_live_remote_handle_for_remote_replay(tmp_path: Path) -> None:
    state = tool_contract_check.SampleState(matrix="remote", rdc_path=tmp_path / "sample.rdc")
    state.rdc_path.write_text("rdc", encoding="utf-8")
    files = {
        "artifacts": tmp_path / "artifacts",
        "sample": tmp_path / "sample.bin",
        "text_a": tmp_path / "a.txt",
        "text_b": tmp_path / "b.txt",
        "png_a": tmp_path / "a.png",
        "png_b": tmp_path / "b.png",
        "zip_out": tmp_path / "bundle.zip",
        "capture_copy": tmp_path / "capture-copy.rdc",
    }

    calls: list[tuple[str, dict[str, object]]] = []

    async def _call(name: str, args: dict[str, object], *, timeout_s: float | None = None):
        calls.append((name, dict(args)))
        if name == "rd.core.init":
            return {"ok": True, "data": {}}, "", ""
        if name == "rd.remote.connect":
            return {"ok": True, "data": {"remote_id": "remote_demo"}}, "", ""
        if name == "rd.remote.ping":
            return {"ok": True, "data": {"pong": True}}, "", ""
        if name == "rd.capture.open_file":
            return {"ok": True, "data": {"capture_file_id": "capf_demo"}}, "", ""
        if name == "rd.capture.open_replay":
            return {"ok": True, "data": {"session_id": "sess_demo"}}, "", ""
        if name == "rd.replay.set_frame":
            return {"ok": True, "data": {"active_event_id": 1}}, "", ""
        if name == "rd.event.get_actions":
            return {"ok": True, "data": {"actions": []}}, "", ""
        if name == "rd.resource.list_textures":
            return {"ok": True, "data": {"textures": []}}, "", ""
        if name == "rd.resource.list_buffers":
            return {"ok": True, "data": {"buffers": []}}, "", ""
        if name == "rd.pipeline.get_shader":
            return {"ok": False, "error": {"code": "shader_not_bound", "message": "not bound"}}, "", ""
        if name == "rd.perf.enumerate_counters":
            return {"ok": True, "data": {"counters": []}}, "", ""
        return {"ok": True, "data": {}}, "", ""

    asyncio.run(tool_contract_check._ensure_context(_call, state, files, need_capture=True, need_remote=False))

    assert state.capture_file_id == "capf_demo"
    assert state.session_id == "sess_demo"
    assert state.live_remote_id == "remote_demo"
    assert ("rd.capture.open_replay", {"capture_file_id": "capf_demo", "options": {"remote_id": "remote_demo"}}) in calls


def test_extract_mcp_payload_and_text_accepts_structured_content() -> None:
    class _StructuredItem:
        def model_dump(self, mode: str = "python"):  # noqa: ARG002
            return {
                "type": "json",
                "data": {
                    "schema_version": "3.0.0",
                    "tool_version": "1.0.0",
                    "result_kind": "rd.core.init",
                    "ok": True,
                    "data": {"ready": True},
                    "artifacts": [],
                    "error": None,
                    "meta": {},
                    "projections": {},
                },
            }

    class _Result:
        content = [_StructuredItem()]

    payload, text = tool_contract_check._extract_mcp_payload_and_text(_Result())

    assert payload is not None
    assert payload["ok"] is True
    assert "rd.core.init" in text
