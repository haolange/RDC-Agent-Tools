from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from rdx import server
from rdx.context_snapshot import clear_context_snapshot


class _FakeActionFlags:
    Drawcall = 1
    Draw = 1
    Dispatch = 2
    MeshDispatch = 4
    DispatchRay = 8
    SetMarker = 16
    PushMarker = 32
    PopMarker = 64
    Copy = 128
    Resolve = 256
    Clear = 512
    Present = 1024
    PassBoundary = 2048


class _FakeShaderStage:
    Vertex = "vs"
    Hull = "hs"
    Domain = "ds"
    Geometry = "gs"
    Pixel = "ps"
    Compute = "cs"
    Mesh = "ms"
    Amplification = "as"


class _FakeAction:
    def __init__(
        self,
        event_id: int,
        *,
        name: str = "",
        flags: int = 0,
        children: list[_FakeAction] | None = None,
        outputs: list[str] | None = None,
    ) -> None:
        self.eventId = event_id
        self.customName = name or f"event_{event_id}"
        self.name = self.customName
        self.flags = flags
        self.children = list(children or [])
        self.outputs = list(outputs or [])
        self.numIndices = 0
        self.numInstances = 1


class _FakeUsageEntry:
    def __init__(self, event_id: int, usage: str) -> None:
        self.eventId = event_id
        self.usage = usage


class _FakePipe:
    def __init__(self, event_id: int) -> None:
        self._event_id = int(event_id)

    def GetShader(self, stage: object) -> str:
        return f"shader@{self._event_id}"

    def GetShaderReflection(self, stage: object) -> SimpleNamespace:
        return SimpleNamespace(entryPoint=f"main_{self._event_id}", encoding="dxil")

    def GetVBuffers(self) -> list[object]:
        return []

    def GetIBuffer(self) -> SimpleNamespace:
        return SimpleNamespace(resourceId="", byteOffset=0, byteStride=0)


class _FakeController:
    def __init__(
        self,
        *,
        roots: list[_FakeAction],
        usage_entries: list[_FakeUsageEntry] | None = None,
        resources: list[object] | None = None,
    ) -> None:
        self._roots = list(roots)
        self._usage_entries = list(usage_entries or [])
        self._resources = list(resources or [])
        self.current_event = 0
        self.set_frame_calls: list[int] = []

    def GetRootActions(self) -> list[_FakeAction]:
        return self._roots

    def SetFrameEvent(self, event_id: int, apply: bool) -> None:
        self.current_event = int(event_id)
        self.set_frame_calls.append(int(event_id))

    def GetPipelineState(self) -> _FakePipe:
        return _FakePipe(self.current_event)

    def GetUsage(self, resource_id: object) -> list[_FakeUsageEntry]:
        return list(self._usage_entries)

    def GetTextures(self) -> list[object]:
        return list(self._resources)

    def GetBuffers(self) -> list[object]:
        return []

    def GetResources(self) -> list[object]:
        return []


class _FakeSnapshot:
    def __init__(self, event_id: int) -> None:
        self._event_id = int(event_id)

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return {
            "api": "Vulkan",
            "shaders": [{"stage": "PS", "shader_id": f"shader@{self._event_id}"}],
            "render_targets": [{"resource_id": f"rt@{self._event_id}"}],
            "bindings": [{"resource_id": f"bind@{self._event_id}"}],
            "topology": "trianglelist",
            "viewport": {"x": 0.0, "y": 0.0},
            "blend_states": [],
            "depth_stencil": {},
            "depth_target": {"resource_id": f"depth@{self._event_id}"},
        }


class _FakeBinding:
    def __init__(self, event_id: int, binding_type: str) -> None:
        self.type = binding_type
        self._event_id = int(event_id)

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return {
            "resource_id": f"{self.type.lower()}@{self._event_id}",
            "type": self.type,
        }


class _FakePipelineService:
    def __init__(self) -> None:
        self.snapshot_calls: list[int] = []
        self.binding_calls: list[int] = []

    async def snapshot_pipeline(self, session_id: str, event_id: int, session_manager: object) -> _FakeSnapshot:
        self.snapshot_calls.append(int(event_id))
        return _FakeSnapshot(event_id)

    async def get_resource_bindings(self, session_id: str, event_id: int, session_manager: object) -> list[_FakeBinding]:
        self.binding_calls.append(int(event_id))
        return [
            _FakeBinding(event_id, "SRV"),
            _FakeBinding(event_id, "UAV"),
        ]


class _FakeSessionManager:
    def __init__(self) -> None:
        self.closed_sessions: list[str] = []

    async def close_session(self, session_id: str) -> None:
        self.closed_sessions.append(str(session_id))


def _install_common_env(monkeypatch: pytest.MonkeyPatch, controller: _FakeController) -> None:
    async def _inline_offload(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        return fn(*args, **kwargs)

    async def _fake_get_controller(session_id: str) -> _FakeController:
        return controller

    fake_rd = SimpleNamespace(
        ActionFlags=_FakeActionFlags,
        ShaderStage=_FakeShaderStage,
    )
    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_get_rd", lambda: fake_rd)


def _seed_capture(capture_file_id: str = "capf_demo") -> None:
    server._runtime.captures = {
        capture_file_id: server.CaptureFileHandle(
            capture_file_id=capture_file_id,
            file_path="capture.rdc",
            read_only=True,
        )
    }


def _seed_session(active_event_id: int, *, session_id: str = "sess_demo", capture_file_id: str = "capf_demo") -> None:
    server._runtime.replays = {
        session_id: server.ReplayHandle(
            session_id=session_id,
            capture_file_id=capture_file_id,
            frame_index=0,
            active_event_id=active_event_id,
        )
    }
    server._set_context_runtime_session(
        session_id,
        capture_file_id=capture_file_id,
        backend_type="local",
        frame_index=0,
        active_event_id=active_event_id,
    )


def _poison_active_event(event_id: int) -> None:
    server._runtime.replays["sess_demo"].active_event_id = int(event_id)
    server._set_context_active_event("sess_demo", int(event_id))


@pytest.fixture(autouse=True)
def _reset_server_state() -> None:
    original_captures = dict(server._runtime.captures)
    original_replays = dict(server._runtime.replays)
    original_contexts = dict(server._runtime.context_snapshots)
    original_pipeline_service = server.server_runtime._pipeline_service
    original_session_manager = server.server_runtime._session_manager
    clear_context_snapshot()
    server._runtime.context_snapshots.clear()
    try:
        yield
    finally:
        clear_context_snapshot()
        server._runtime.context_snapshots.clear()
        server._runtime.captures = original_captures
        server._runtime.replays = original_replays
        server._runtime.context_snapshots.update(original_contexts)
        server.server_runtime._pipeline_service = original_pipeline_service
        server.server_runtime._session_manager = original_session_manager


def test_event_set_active_validates_before_mutating_state(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _FakeController(
        roots=[
            _FakeAction(101, flags=_FakeActionFlags.Drawcall),
            _FakeAction(202, flags=_FakeActionFlags.Drawcall),
        ]
    )
    _install_common_env(monkeypatch, controller)
    _seed_capture()
    _seed_session(202)

    invalid = asyncio.run(server.dispatch_operation("rd.event.set_active", {"session_id": "sess_demo", "event_id": 53}, transport="test"))
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "event_not_found"
    assert invalid["error"]["category"] == "not_found"
    assert invalid["error"]["details"] == {"session_id": "sess_demo", "event_id": 53}
    assert controller.set_frame_calls == []
    assert server._runtime.replays["sess_demo"].active_event_id == 202
    assert server._context_snapshot()["runtime"]["active_event_id"] == 202

    valid = asyncio.run(server.dispatch_operation("rd.event.set_active", {"session_id": "sess_demo", "event_id": 101}, transport="test"))
    assert valid["ok"] is True
    assert valid["data"]["active_event_id"] == 101
    assert controller.set_frame_calls == [101]
    assert server._runtime.replays["sess_demo"].active_event_id == 101
    assert server._context_snapshot()["runtime"]["active_event_id"] == 101


def test_get_action_tree_paginates_and_bounds_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _FakeController(
        roots=[
            _FakeAction(101, name="root_a", children=[_FakeAction(111, name="child_a")]),
            _FakeAction(201, name="root_b", children=[_FakeAction(211, name="child_b")]),
            _FakeAction(301, name="root_c", children=[_FakeAction(311, name="child_c")]),
        ]
    )
    _install_common_env(monkeypatch, controller)
    _seed_capture()
    _seed_session(101)

    payload = json.loads(
        asyncio.run(
            server._dispatch_event(
                "get_action_tree",
                {"session_id": "sess_demo", "offset": 1, "limit": 1, "max_nodes": 2},
            )
        )
    )

    assert payload["success"] is True
    assert payload["pagination"] == {
        "offset": 1,
        "limit": 1,
        "max_nodes": 2,
        "returned_root_count": 1,
        "total_root_count": 3,
        "truncated": True,
    }
    assert payload["root"]["children"][0]["event_id"] == 201
    assert payload["root"]["children"][0]["children"][0]["event_id"] == 211


def test_get_actions_reports_root_browse_lookup_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _FakeController(
        roots=[
            _FakeAction(101, name="root_a", children=[_FakeAction(111, name="child_a")]),
        ]
    )
    _install_common_env(monkeypatch, controller)
    _seed_capture()
    _seed_session(101)

    payload = json.loads(asyncio.run(server._dispatch_event("get_actions", {"session_id": "sess_demo"})))

    assert payload["success"] is True
    assert payload["lookup_scope"] == "root_browse"
    assert payload["recommended_followup_tool"] == "rd.event.get_action_tree"


def test_ensure_event_repairs_polluted_active_event(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _FakeController(
        roots=[
            _FakeAction(101, flags=_FakeActionFlags.Drawcall),
            _FakeAction(202, flags=_FakeActionFlags.Drawcall),
        ]
    )
    _install_common_env(monkeypatch, controller)
    _seed_capture()
    _seed_session(53)

    resolved = asyncio.run(server._ensure_event("sess_demo", None))
    assert resolved == 101
    assert controller.set_frame_calls == [101]
    assert server._runtime.replays["sess_demo"].active_event_id == 101
    assert server._context_snapshot()["runtime"]["active_event_id"] == 101


def test_pipeline_dispatch_uses_one_resolved_event_context(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _FakeController(
        roots=[
            _FakeAction(101, flags=_FakeActionFlags.Drawcall, outputs=["rt0"]),
            _FakeAction(202, flags=_FakeActionFlags.Drawcall, outputs=["rt1"]),
        ]
    )
    pipeline_service = _FakePipelineService()
    _install_common_env(monkeypatch, controller)
    server.server_runtime._pipeline_service = pipeline_service
    server.server_runtime._session_manager = SimpleNamespace()
    _seed_capture()
    _seed_session(53)

    summary = json.loads(asyncio.run(server._dispatch_pipeline("get_state_summary", {"session_id": "sess_demo"})))
    assert summary["success"] is True
    assert summary["summary"]["render_targets"][0]["resource_id"] == "rt@101"
    assert summary["summary"]["summary_status"] == "verified"
    assert summary["summary"]["binding_truth_level"] == "binding_verified"
    assert pipeline_service.snapshot_calls == [101]

    _poison_active_event(53)
    render_targets = json.loads(asyncio.run(server._dispatch_pipeline("get_render_targets", {"session_id": "sess_demo"})))
    assert render_targets["success"] is True
    assert render_targets["render_targets"][0]["resource_id"] == "rt@101"
    assert render_targets["binding_truth_level"] == "binding_verified"
    assert pipeline_service.snapshot_calls == [101, 101]

    _poison_active_event(53)
    output_targets = json.loads(asyncio.run(server._dispatch_pipeline("get_output_targets", {"session_id": "sess_demo"})))
    assert output_targets["success"] is True
    assert output_targets["framebuffer"]["depth_target"]["resource_id"] == "depth@101"
    assert output_targets["framebuffer"]["summary_status"] == "verified"
    assert pipeline_service.snapshot_calls == [101, 101, 101]

    _poison_active_event(53)
    shader = json.loads(asyncio.run(server._dispatch_pipeline("get_shader", {"session_id": "sess_demo", "stage": "ps"})))
    assert shader["success"] is True
    assert shader["shader"]["shader_id"] == "shader@101"
    assert pipeline_service.snapshot_calls == [101, 101, 101, 101]

    _poison_active_event(53)
    bindings = json.loads(asyncio.run(server._dispatch_pipeline("get_resource_bindings", {"session_id": "sess_demo"})))
    assert bindings["success"] is True
    assert bindings["bindings"][0]["resource_id"] == "srv@101"
    assert pipeline_service.snapshot_calls == [101, 101, 101, 101, 101]
    assert pipeline_service.binding_calls == [101]


def test_pipeline_summary_and_outputs_include_selected_visual_target(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _FakeController(
        roots=[
            _FakeAction(101, flags=_FakeActionFlags.Drawcall, outputs=[]),
        ]
    )
    pipeline_service = _FakePipelineService()
    _install_common_env(monkeypatch, controller)
    server.server_runtime._pipeline_service = pipeline_service
    server.server_runtime._session_manager = SimpleNamespace()
    _seed_capture()
    _seed_session(101)

    async def _fake_resolve_visual_target_for_event(session_id: str, event_id: int, *, target=None, allow_framebuffer_fallback=True):  # type: ignore[no-untyped-def]
        return (
            "ResourceId::777",
            SimpleNamespace(name="SceneColor"),
            {
                "texture_id": "ResourceId::777",
                "output_slot": None,
                "target_source": "event_binding_uav_3",
                "texture_format": "R8G8B8A8_UNORM",
            },
            {
                "summary_status": "degraded",
                "summary_degraded_reasons": ["visual_target_binding_fallback"],
                "binding_truth_level": "binding_degraded",
                "evidence_truth_level": "visual_evidence_only",
            },
        )

    monkeypatch.setattr(server.server_runtime, "_resolve_visual_target_for_event", _fake_resolve_visual_target_for_event)

    summary = json.loads(asyncio.run(server._dispatch_pipeline("get_state_summary", {"session_id": "sess_demo"})))
    outputs = json.loads(asyncio.run(server._dispatch_pipeline("get_output_targets", {"session_id": "sess_demo"})))

    assert summary["success"] is True
    assert summary["summary"]["selected_visual_target"]["target_source"] == "event_binding_uav_3"
    assert summary["summary"]["export_target_available"] is True
    assert outputs["success"] is True
    assert outputs["framebuffer"]["selected_visual_target"]["texture_id"] == "ResourceId::777"
    assert outputs["framebuffer"]["export_target_available"] is True


def test_pipeline_get_constant_buffers_uses_collected_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _FakeController(
        roots=[
            _FakeAction(101, flags=_FakeActionFlags.Drawcall),
        ]
    )
    pipeline_service = _FakePipelineService()
    _install_common_env(monkeypatch, controller)
    server.server_runtime._pipeline_service = pipeline_service
    server.server_runtime._session_manager = SimpleNamespace()
    _seed_capture()
    _seed_session(101)

    async def _fake_collect_constant_buffers(*args, **kwargs):  # type: ignore[no-untyped-def]
        return [
            {
                "stage": "PS",
                "slot": 2,
                "resource_id": "ResourceId::cb0",
                "offset": 16,
                "size": 64,
                "block_name": "Globals",
                "vars": [{"name": "Tint", "type": "float4"}],
                "contents": [{"name": "Tint", "value": {"f32v": [1.0, 0.0, 0.0, 1.0]}}],
            }
        ]

    monkeypatch.setattr(server.server_runtime, "_collect_constant_buffers", _fake_collect_constant_buffers)

    payload = json.loads(asyncio.run(server._dispatch_pipeline("get_constant_buffers", {"session_id": "sess_demo", "stage": "ps", "include_contents": True})))

    assert payload["success"] is True
    assert payload["constant_buffers"][0]["slot"] == 2
    assert payload["constant_buffers"][0]["contents"][0]["name"] == "Tint"


def test_pipeline_get_shader_returns_runtime_error_when_stage_is_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NullShaderPipe(_FakePipe):
        def GetShader(self, stage: object) -> str:
            return "ResourceId::0"

    class _NullShaderController(_FakeController):
        def GetPipelineState(self) -> _NullShaderPipe:
            return _NullShaderPipe(self.current_event)

    controller = _NullShaderController(
        roots=[
            _FakeAction(101, flags=_FakeActionFlags.Drawcall, outputs=["rt0"]),
        ]
    )
    pipeline_service = _FakePipelineService()
    _install_common_env(monkeypatch, controller)
    monkeypatch.setattr(server.server_runtime, "_is_null_resource_id", lambda rid: str(rid) in {"", "ResourceId::0", "0"})
    server.server_runtime._pipeline_service = pipeline_service
    server.server_runtime._session_manager = SimpleNamespace()
    _seed_capture()
    _seed_session(101)

    payload = json.loads(asyncio.run(server._dispatch_pipeline("get_shader", {"session_id": "sess_demo", "stage": "ps"})))

    assert payload["success"] is False
    assert payload["code"] == "shader_not_bound"
    assert payload["details"]["resolved_event_id"] == 101
    assert payload["details"]["stage"] == "PS"


def test_resource_usage_and_history_expose_canonical_and_raw_event_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _FakeController(
        roots=[
            _FakeAction(101, flags=_FakeActionFlags.Drawcall),
            _FakeAction(202, flags=_FakeActionFlags.Drawcall),
        ],
        usage_entries=[
            _FakeUsageEntry(101, "Read"),
            _FakeUsageEntry(53, "Write"),
            _FakeUsageEntry(1042, "Read"),
        ],
        resources=[
            SimpleNamespace(
                resourceId="ResourceId::7",
                name="main_color",
                width=1,
                height=1,
                depth=1,
                mips=1,
                format=SimpleNamespace(Name=lambda: "R8G8B8A8_UNORM"),
            )
        ],
    )
    _install_common_env(monkeypatch, controller)
    _seed_capture()
    _seed_session(101)

    usage = json.loads(
        asyncio.run(
            server._dispatch_resource(
                "get_usage",
                {"session_id": "sess_demo", "resource_id": "ResourceId::7", "max_events": 10},
            )
        )
    )
    assert usage["success"] is True
    assert usage["usage"] == [
        {"event_id": 101, "raw_event_id": 101, "event_resolvable": True, "usage": "Read"},
        {"event_id": None, "raw_event_id": 53, "event_resolvable": False, "usage": "Write"},
        {"event_id": None, "raw_event_id": 1042, "event_resolvable": False, "usage": "Read"},
    ]

    history = json.loads(
        asyncio.run(
            server._dispatch_resource(
                "get_history",
                {"session_id": "sess_demo", "resource_id": "ResourceId::7"},
            )
        )
    )
    assert history["success"] is True
    assert history["history"] == [
        {"event_id": 101, "raw_event_id": 101, "event_resolvable": True, "usage": "Read", "is_write": False},
        {"event_id": None, "raw_event_id": 53, "event_resolvable": False, "usage": "Write", "is_write": True},
        {"event_id": None, "raw_event_id": 1042, "event_resolvable": False, "usage": "Read", "is_write": False},
    ]



def test_capture_close_file_rejects_unknown_handle() -> None:
    payload = json.loads(asyncio.run(server._dispatch_capture("close_file", {"capture_file_id": "capf_missing"})))
    assert payload["success"] is False
    assert payload["error_message"] == "Unknown capture_file_id: capf_missing"



def test_capture_close_file_rejects_when_replay_depends_on_capture() -> None:
    _seed_capture()
    _seed_session(101)

    payload = json.loads(asyncio.run(server._dispatch_capture("close_file", {"capture_file_id": "capf_demo"})))
    assert payload["success"] is False
    assert payload["code"] == "capture_file_in_use"
    assert payload["category"] == "runtime"
    assert payload["details"] == {
        "capture_file_id": "capf_demo",
        "dependent_session_ids": ["sess_demo"],
        "dependent_session_count": 1,
    }
    assert "capf_demo" in server._runtime.captures
    assert server._runtime.replays["sess_demo"].capture_file_id == "capf_demo"
    assert server._context_snapshot()["runtime"]["capture_file_id"] == "capf_demo"



def test_capture_close_file_reports_all_dependent_sessions() -> None:
    _seed_capture()
    server._runtime.replays = {
        "sess_b": server.ReplayHandle(session_id="sess_b", capture_file_id="capf_demo", frame_index=0, active_event_id=0),
        "sess_a": server.ReplayHandle(session_id="sess_a", capture_file_id="capf_demo", frame_index=0, active_event_id=0),
    }
    server._set_context_runtime_session(
        "sess_a",
        capture_file_id="capf_demo",
        backend_type="local",
        frame_index=0,
        active_event_id=0,
    )

    payload = json.loads(asyncio.run(server._dispatch_capture("close_file", {"capture_file_id": "capf_demo"})))
    assert payload["success"] is False
    assert payload["details"]["dependent_session_ids"] == ["sess_a", "sess_b"]
    assert payload["details"]["dependent_session_count"] == 2
    assert "capf_demo" in server._runtime.captures



def test_capture_close_file_succeeds_after_close_replay() -> None:
    manager = _FakeSessionManager()
    server.server_runtime._session_manager = manager
    _seed_capture()
    _seed_session(101)

    close_replay = json.loads(asyncio.run(server._dispatch_capture("close_replay", {"session_id": "sess_demo"})))
    assert close_replay["success"] is True
    assert manager.closed_sessions == ["sess_demo"]
    assert server._runtime.replays == {}
    assert "capf_demo" in server._runtime.captures

    close_file = json.loads(asyncio.run(server._dispatch_capture("close_file", {"capture_file_id": "capf_demo"})))
    assert close_file["success"] is True
    assert server._runtime.captures == {}
    context = server._context_snapshot()
    assert context["runtime"]["session_id"] == ""
    assert context["runtime"]["capture_file_id"] == ""


def test_export_shader_bundle_forwards_requested_event(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    seen_calls: list[dict[str, Any]] = []

    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        assert session_id == "sess_demo"
        return int(event_id or 0)

    async def _fake_dispatch_pipeline(action: str, args: dict[str, object]) -> str:
        seen_calls.append({"action": action, **dict(args)})
        stage = str(args["stage"])
        event_id = int(args["event_id"])
        return json.dumps(
            {
                "success": True,
                "shader": {
                    "stage": stage.upper(),
                    "shader_id": f"{stage}@{event_id}",
                    "entry_point": f"main_{event_id}",
                },
                "resolved_event_id": event_id,
            }
        )

    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_dispatch_pipeline", _fake_dispatch_pipeline)

    output_dir = tmp_path / "shader_bundle"
    payload = json.loads(
        asyncio.run(
            server._dispatch_export(
                "shader_bundle",
                {"session_id": "sess_demo", "event_id": 202, "output_dir": str(output_dir)},
            )
        )
    )

    assert payload["success"] is True
    assert payload["resolved_event_id"] == 202
    assert seen_calls
    assert all(call["action"] == "get_shader" for call in seen_calls)
    assert all(int(call["event_id"]) == 202 for call in seen_calls)
    bundle = json.loads((output_dir / "shader_bundle.json").read_text(encoding="utf-8"))
    assert bundle["requested_event_id"] == 202
    assert bundle["resolved_event_id"] == 202
    assert bundle["shaders"][0]["resolved_event_id"] == 202


def test_export_screenshot_forwards_requested_event(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    seen_calls: list[dict[str, object]] = []

    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        assert session_id == "sess_demo"
        return int(event_id or 0)

    async def _fake_output_target_resource_ids(session_id: str, event_id: int) -> list[tuple[str, int]]:
        assert session_id == "sess_demo"
        assert event_id == 202
        return [("ResourceId::77", 0)]

    async def _fake_get_texture_descriptor(session_id: str, texture_id: object, *, event_id: int | None = None):
        assert session_id == "sess_demo"
        assert int(event_id or 0) == 202
        return str(texture_id), SimpleNamespace(name="main_color")

    async def _fake_dispatch_texture(action: str, args: dict[str, object]) -> str:
        seen_calls.append({"action": action, **dict(args)})
        return json.dumps(
            {
                "success": True,
                "artifact_path": str(tmp_path / "shot.png"),
                "saved_path": str(tmp_path / "shot.png"),
                "image_path": str(tmp_path / "shot.png"),
                "meta": {"event_id": int(args["event_id"])},
            }
        )

    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_output_target_resource_ids", _fake_output_target_resource_ids)
    monkeypatch.setattr(server.server_runtime, "_get_texture_descriptor", _fake_get_texture_descriptor)
    monkeypatch.setattr(server.server_runtime, "_binding_name_index_for_event", lambda session_id, event_id: asyncio.sleep(0, result={}))
    monkeypatch.setattr(server.server_runtime, "_dispatch_texture", _fake_dispatch_texture)
    monkeypatch.setattr(server.server_runtime, "_recommend_formats_for_texture", lambda texture_desc, name_info, for_screenshot=False: ["png"])
    monkeypatch.setattr(
        server.server_runtime,
        "_render_service",
        SimpleNamespace(
            get_texture_stats=lambda **kwargs: asyncio.sleep(
                0,
                result={
                    "channels": {
                        "r": {"min": 0.0, "max": 1.0},
                        "g": {"min": 0.0, "max": 1.0},
                        "b": {"min": 0.0, "max": 1.0},
                        "a": {"min": 1.0, "max": 1.0},
                    },
                    "has_any_nan": False,
                    "has_any_inf": False,
                },
            )
        ),
    )

    payload = json.loads(
        asyncio.run(
            server._dispatch_export(
                "screenshot",
                {"session_id": "sess_demo", "event_id": 202, "output_path": str(tmp_path / "shot.png")},
            )
        )
    )

    assert payload["success"] is True
    assert payload["meta"]["event_id"] == 202
    assert seen_calls
    assert all(call["action"] == "render_overlay" for call in seen_calls)
    assert all(int(call["event_id"]) == 202 for call in seen_calls)


def test_export_shader_bundle_returns_runtime_error_when_event_has_no_bound_shaders(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        return int(event_id or 0)

    async def _fake_dispatch_pipeline(action: str, args: dict[str, object]) -> str:
        return json.dumps(
            {
                "success": False,
                "code": "shader_not_bound",
                "category": "runtime",
                "details": {
                    "session_id": str(args["session_id"]),
                    "resolved_event_id": int(args["event_id"]),
                    "stage": str(args["stage"]).upper(),
                },
            }
        )

    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_dispatch_pipeline", _fake_dispatch_pipeline)

    output_dir = tmp_path / "shader_bundle_empty"
    payload = json.loads(
        asyncio.run(
            server._dispatch_export(
                "shader_bundle",
                {"session_id": "sess_demo", "event_id": 202, "output_dir": str(output_dir)},
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "shader_bundle_empty"
    assert payload["details"]["resolved_event_id"] == 202
