from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from rdx import server


class _FakeTrace:
    def __init__(self, *, valid: bool, debugger: object | None = None) -> None:
        self.valid = valid
        self.debugger = object() if debugger is None and not valid else debugger


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
    assert payload["code"] == "shader_debug_event_binding_unavailable"
    assert payload["category"] == "capability"
    assert payload["details"]["capability"] == "shader_debug"
    assert "resolved_context" in payload["details"]
    assert "pixel_history_summary" in payload["details"]
    assert "attempts" in payload["details"]
    assert "selected_target_source" in payload["details"]
    assert payload["details"]["failure_stage"] == "debug_pixel"
    assert payload["details"]["failure_reason"] == "invalid_trace"
    assert payload["details"]["debug_attempt_count"] == 1
    assert controller.debug_calls[0] == {"x": 3, "y": 4, "sample": 7, "view": 8, "primitive": 9}


def test_debug_start_rejects_cross_event_fallback(monkeypatch) -> None:
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
    assert start["success"] is False
    assert start["code"] == "shader_debug_event_binding_unavailable"
    assert start["details"]["capability"] == "shader_debug"
    assert start["details"]["failure_stage"] == "pixel_history"
    assert start["details"]["failure_reason"] == "cross_event_only"
    assert any(
        "Cross-event shader debug fallback" in str(item.get("error") or "")
        for item in start["details"]["attempts"]
        if isinstance(item, dict)
    )


def test_debug_start_reports_target_configuration_failures(monkeypatch) -> None:
    controller = _FakeController([])
    _install_debug_env(monkeypatch, controller, [])

    async def _failing_configure(session_id: str, target: dict[str, object], *, event_id: int | None = None, sample_override: int | None = None):  # type: ignore[no-untyped-def]
        raise RuntimeError("no target available")

    monkeypatch.setattr(server.server_runtime, "_configure_texture_output_for_target", _failing_configure)

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "debug_start",
                {
                    "session_id": "sess_test",
                    "mode": "pixel",
                    "event_id": 11,
                    "params": {"x": 2, "y": 3, "target": {"texture_id": "ResourceId::77"}},
                },
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "shader_debug_target_config_failed"
    assert payload["category"] == "runtime"
    assert payload["details"]["failure_stage"] == "configure_target"
    assert payload["details"]["failure_reason"] == "all_targets_failed"
    assert payload["details"]["target_config_failures"] >= 1


def test_debug_start_reports_missing_debugger_handle(monkeypatch) -> None:
    controller = _FakeController([_FakeTrace(valid=True, debugger=None)])
    _install_debug_env(monkeypatch, controller, [])

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "debug_start",
                {
                    "session_id": "sess_test",
                    "mode": "pixel",
                    "event_id": 11,
                    "params": {"x": 5, "y": 6, "target": {"texture_id": "ResourceId::77"}},
                },
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "shader_debug_start_failed"
    assert payload["category"] == "runtime"
    assert payload["details"]["failure_stage"] == "trace_state"
    assert payload["details"]["failure_reason"] == "debugger_handle_missing"


def test_debug_start_times_out_pixel_history(monkeypatch) -> None:
    controller = _FakeController([])
    _install_debug_env(monkeypatch, controller, [])

    async def _slow_history(controller_obj: object, resource_id: object, x: int, y: int, subresource: object):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.05)
        return []

    monkeypatch.setattr(server.server_runtime, "_pixel_history_raw", _slow_history)
    monkeypatch.setattr(server.server_runtime, "PIXEL_HISTORY_TIMEOUT_S", 0.01)

    payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "debug_start",
                {
                    "session_id": "sess_test",
                    "mode": "pixel",
                    "event_id": 11,
                    "params": {"x": 5, "y": 6, "target": {"texture_id": "ResourceId::77"}},
                },
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "shader_debug_start_failed"
    assert payload["category"] == "runtime"
    assert payload["details"]["failure_stage"] == "pixel_history"
    assert payload["details"]["failure_reason"] == "pixel_history_timeout"
    assert payload["details"]["pixel_history_timeout_count"] == 1


def test_texture_get_pixel_history_times_out(monkeypatch) -> None:
    class _FakeSubresource:
        def __init__(self) -> None:
            self.mip = 0
            self.slice = 0
            self.sample = 0

    async def _fake_get_controller(session_id: str) -> object:
        return object()

    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        return int(event_id or 11)

    async def _fake_resolve_texture_id(session_id: str, texture_id: object, *, event_id: int | None = None) -> str:
        return str(texture_id)

    async def _slow_history(controller_obj: object, resource_id: object, x: int, y: int, subresource: object):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.05)
        return []

    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_resolve_texture_id", _fake_resolve_texture_id)
    monkeypatch.setattr(server.server_runtime, "_pixel_history_raw", _slow_history)
    monkeypatch.setattr(server.server_runtime, "PIXEL_HISTORY_TIMEOUT_S", 0.01)
    monkeypatch.setattr(server.server_runtime, "_get_rd", lambda: SimpleNamespace(Subresource=_FakeSubresource))
    monkeypatch.setattr(server.server_runtime, "_render_service", object())

    payload = json.loads(
        asyncio.run(
            server._dispatch_texture(
                "get_pixel_history",
                {"session_id": "sess_test", "texture_id": "ResourceId::77", "x": 1, "y": 2},
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "pixel_history_timeout"
    assert payload["category"] == "runtime"
    assert payload["details"]["timeout_seconds"] == 0.01
    assert payload["details"]["resolved_event_id"] == 11


def test_macro_shader_hotfix_validate_uses_validation_target_and_metrics(monkeypatch, tmp_path) -> None:
    export_calls: list[dict[str, object]] = []
    texture_calls: list[dict[str, object]] = []
    util_calls: list[dict[str, object]] = []

    async def _fake_export(action: str, args: dict[str, object]) -> str:
        export_calls.append({"action": action, **dict(args)})
        output_path = Path(str(args["output_path"]))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"png")
        target = args.get("target") if isinstance(args.get("target"), dict) else {}
        texture_id = str((target or {}).get("texture_id") or "ResourceId::fallback")
        return json.dumps(
            {
                "success": True,
                "saved_path": str(output_path),
                "image_path": str(output_path),
                "meta": {"event_id": int(args.get("event_id") or 0), "texture_id": texture_id},
            }
        )

    async def _fake_shader(action: str, args: dict[str, object]) -> str:
        assert action == "edit_and_replace"
        return json.dumps({"success": True, "replacement_id": "repl_demo"})

    async def _fake_texture(action: str, args: dict[str, object]) -> str:
        texture_calls.append({"action": action, **dict(args)})
        return json.dumps(
            {
                "success": True,
                "pixel": {
                    "texture_id": str(args["texture_id"]),
                    "x": int(args["x"]),
                    "y": int(args["y"]),
                },
            }
        )

    async def _fake_util(action: str, args: dict[str, object]) -> str:
        util_calls.append({"action": action, **dict(args)})
        return json.dumps({"success": True, "metrics": {"mse": 0.0}})

    monkeypatch.setattr(server.server_runtime, "_dispatch_export", _fake_export)
    monkeypatch.setattr(server.server_runtime, "_dispatch_shader", _fake_shader)
    monkeypatch.setattr(server.server_runtime, "_dispatch_texture", _fake_texture)
    monkeypatch.setattr(server.server_runtime, "_dispatch_util", _fake_util)

    payload = json.loads(
        asyncio.run(
            server._dispatch_macro(
                "shader_hotfix_validate",
                {
                    "session_id": "sess_test",
                    "replacement": {
                        "event_id": 1248,
                        "stage": "ps",
                        "shader_id": "ResourceId::192587",
                        "ops": [{"op": "force_full_precision"}],
                    },
                    "validation": {
                        "target_texture_id": "ResourceId::208592",
                        "x": 754,
                        "y": 350,
                        "metric": "mse",
                    },
                    "output_dir": str(tmp_path),
                },
            )
        )
    )

    assert payload["success"] is True
    assert len(export_calls) == 2
    assert export_calls[0]["target"] == {"texture_id": "ResourceId::208592"}
    assert export_calls[1]["target"] == {"texture_id": "ResourceId::208592"}
    assert payload["before"]["meta"]["texture_id"] == "ResourceId::208592"
    assert payload["after"]["meta"]["texture_id"] == "ResourceId::208592"
    assert payload["validation"]["before_pixel"]["pixel"]["texture_id"] == "ResourceId::208592"
    assert payload["validation"]["after_pixel"]["pixel"]["texture_id"] == "ResourceId::208592"
    assert len(texture_calls) == 2
    assert len(util_calls) == 1
    assert util_calls[0]["metrics"] == ["mse"]


def test_capabilities_and_compile_error_are_structured() -> None:
    caps = asyncio.run(server._core_capabilities(detail="full"))
    assert "app_api" not in caps
    for key in ("remote", "shader_debug", "shader_replace", "mesh_post_transform", "shader_binary_export", "shader_compile", "counters"):
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
        assert payload["error"]["code"] == "validation_error"
        assert "session_id" in str(payload["error"]["message"])
    finally:
        asyncio.run(server.runtime_shutdown())
