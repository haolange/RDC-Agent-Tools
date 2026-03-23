from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from rdx import server


class _FakeTrace:
    def __init__(self, *, valid: bool) -> None:
        self.valid = valid
        self.debugger = None


class _FakeHistoryItem:
    def __init__(self, event_id: int, primitive_id: int, *, passed: bool = True) -> None:
        self.eventId = event_id
        self.primitiveID = primitive_id
        self.fragIndex = 0
        self.depthTestFailed = False
        self.stencilTestFailed = False
        self.shaderDiscarded = False
        self.unboundPS = False
        self.sampleMasked = False
        self.scissorClipped = False
        self.viewClipped = False
        self.backfaceCulled = False
        self.directShaderWrite = False
        self._passed = passed

    def Passed(self) -> bool:
        return self._passed


class _FakeController:
    def __init__(self, traces: list[_FakeTrace]) -> None:
        self._traces = list(traces)
        self.debug_calls: list[dict[str, int | None]] = []

    def GetPipelineState(self) -> object:
        return object()

    def DebugPixel(self, x: int, y: int, inputs: object) -> _FakeTrace:
        self.debug_calls.append(
            {
                "x": x,
                "y": y,
                "sample": getattr(inputs, "sample", None),
                "view": getattr(inputs, "view", None),
                "primitive": getattr(inputs, "primitive", None),
            }
        )
        if self._traces:
            return self._traces.pop(0)
        return _FakeTrace(valid=False)

    def FreeTrace(self, trace: object) -> None:
        return None

    def ContinueDebug(self, debugger: object) -> list[object]:
        raise AssertionError("synthetic fallback should not call ContinueDebug")


class _FakeOutput:
    def SetPixelContextLocation(self, x: int, y: int) -> None:
        return None

    def Display(self) -> None:
        return None


class _FakeDebugInputs:
    pass


def _install_debug_env(monkeypatch, controller: _FakeController, history_items: list[_FakeHistoryItem]) -> None:
    async def _fake_offload(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        return fn(*args, **kwargs)

    async def _fake_get_controller(session_id: str) -> _FakeController:
        return controller

    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        return int(event_id or 0)

    async def _fake_configure(session_id: str, target: dict[str, object], *, event_id: int | None = None, sample_override: int | None = None):  # type: ignore[no-untyped-def]
        subresource = dict(target.get("subresource") or {})
        sample_value = sample_override if sample_override is not None else int(subresource.get("sample", 0) or 0)
        return (
            "ResourceId::77",
            None,
            SimpleNamespace(
                mip=int(subresource.get("mip", 0) or 0),
                slice=int(subresource.get("slice", 0) or 0),
                sample=sample_value,
            ),
        )

    async def _fake_history(controller_obj: object, resource_id: object, x: int, y: int, subresource: object):  # type: ignore[no-untyped-def]
        return history_items

    monkeypatch.setattr(server.server_runtime, "_offload", _fake_offload)
    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_configure_texture_output_for_target", _fake_configure)
    monkeypatch.setattr(server.server_runtime, "_pixel_history_raw", _fake_history)
    monkeypatch.setattr(server.server_runtime, "_output_target_resource_ids", lambda session_id, event_id: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(server.server_runtime, "_refresh_pixel_context", lambda session_id, x, y: asyncio.sleep(0))
    monkeypatch.setattr(server.server_runtime, "_get_rd", lambda: SimpleNamespace(DebugPixelInputs=_FakeDebugInputs))
    monkeypatch.setattr(server.server_runtime, "_session_manager", SimpleNamespace(get_output=lambda session_id: _FakeOutput()))
    server._runtime.shader_debugs.clear()


def test_debug_start_returns_structured_error_details(monkeypatch) -> None:
    controller = _FakeController([_FakeTrace(valid=False)])
    _install_debug_env(monkeypatch, controller, [])

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "debug_start",
                {
                    "session_id": "sess_test",
                    "mode": "pixel",
                    "event_id": 11,
                    "params": {
                        "x": 3,
                        "y": 4,
                        "sample": 7,
                        "view": 8,
                        "primitive": 9,
                        "target": {"rt_index": 2, "subresource": {"mip": 1, "slice": 2, "sample": 3}},
                    },
                },
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "sample_compatibility"
    assert payload["category"] == "runtime"
    assert "resolved_context" in payload["details"]
    assert "pixel_history_summary" in payload["details"]
    assert "attempts" in payload["details"]
    assert "selected_target_source" in payload["details"]
    assert controller.debug_calls[0] == {"x": 3, "y": 4, "sample": 7, "view": 8, "primitive": 9}


def test_debug_start_synthetic_fallback_unlocks_debug_chain(monkeypatch) -> None:
    controller = _FakeController([_FakeTrace(valid=False), _FakeTrace(valid=False), _FakeTrace(valid=False)])
    _install_debug_env(monkeypatch, controller, [_FakeHistoryItem(17, 0)])

    start = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "debug_start",
                {
                    "session_id": "sess_test",
                    "mode": "pixel",
                    "event_id": 11,
                    "params": {"x": 1, "y": 1, "target": {"texture_id": "ResourceId::77"}},
                },
            )
        )
    )
    assert start["success"] is True
    assert start["synthetic_debug"] is True
    assert start["resolved_context"]["event_id"] == 17
    assert start["resolved_context"]["primitive"] == 0

    shader_debug_id = start["shader_debug_id"]
    state = json.loads(asyncio.run(server._dispatch_shader("get_debug_state", {"session_id": "sess_test", "shader_debug_id": shader_debug_id})))
    assert state["success"] is True
    assert state["resolved_context"]["debug_backend"] == "synthetic"

    step = json.loads(asyncio.run(server._dispatch_debug("step", {"session_id": "sess_test", "shader_debug_id": shader_debug_id})))
    assert step["success"] is True

    run_to = json.loads(
        asyncio.run(
            server._dispatch_debug(
                "run_to",
                {"session_id": "sess_test", "shader_debug_id": shader_debug_id, "target": {"pc": 0}, "timeout_ms": 50},
            )
        )
    )
    assert run_to["success"] is True

    variables = json.loads(asyncio.run(server._dispatch_debug("get_variables", {"session_id": "sess_test", "shader_debug_id": shader_debug_id})))
    assert variables["success"] is True
    assert any(item["name"] == "primitive" for item in variables["variables"])

    expr = json.loads(
        asyncio.run(server._dispatch_debug("evaluate_expression", {"session_id": "sess_test", "shader_debug_id": shader_debug_id, "expression": "0"}))
    )
    assert expr["success"] is True
    assert expr["value"] == 0

    finish = json.loads(asyncio.run(server._dispatch_debug("finish", {"session_id": "sess_test", "shader_debug_id": shader_debug_id})))
    assert finish["success"] is True
    assert shader_debug_id not in server._runtime.shader_debugs


def test_capabilities_and_compile_error_are_structured() -> None:
    caps = asyncio.run(server._core_capabilities(detail="full"))
    assert "app_api" not in caps
    for key in ("remote", "shader_debug", "mesh_post_transform", "shader_binary_export", "shader_compile", "counters"):
        entry = caps[key]
        assert set(("available", "reason", "optional", "source")).issubset(entry.keys())

    payload = asyncio.run(
        server.dispatch_operation(
            "rd.shader.compile",
            {
                "source": "float4 main() : SV_Target { return 0; }",
                "stage": "ps",
                "entry": "main",
                "target": "ps_5_0",
                "defines": {},
                "include_dirs": [],
                "additional_args": [],
                "output_path": "intermediate/artifacts/test_compile.out",
            },
            transport="core",
        )
    )
    try:
        assert payload["ok"] is False
        assert payload["error"]["code"] == "shader_compile_unavailable"
        assert payload["error"]["details"]["capability"] == "shader_compile"
    finally:
        asyncio.run(server.runtime_shutdown())
