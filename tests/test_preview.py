from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rdx import server
from rdx.context_snapshot import clear_context_snapshot, load_context_snapshot
from rdx.preview_window import fit_content_rect, fit_size_within_bounds
from rdx.runtime_state import clear_context_state, load_context_state, save_context_state


@pytest.fixture(autouse=True)
def _reset_preview_runtime() -> None:
    original_snapshots = dict(server._runtime.context_snapshots)
    original_states = dict(server._runtime.context_states)
    original_previews = dict(server._runtime.previews)
    original_hydrated = set(server._runtime.hydrated_contexts)
    original_logs = list(server._runtime.logs)
    for context_id in ("default", "ctx-preview"):
        clear_context_snapshot(context_id)
        clear_context_state(context_id)
    server._runtime.context_snapshots.clear()
    server._runtime.context_states.clear()
    server._runtime.previews.clear()
    server._runtime.hydrated_contexts.clear()
    server._runtime.logs.clear()
    try:
        yield
    finally:
        for context_id in ("default", "ctx-preview"):
            clear_context_snapshot(context_id)
            clear_context_state(context_id)
        server._runtime.context_snapshots = original_snapshots
        server._runtime.context_states = original_states
        server._runtime.previews = original_previews
        server._runtime.hydrated_contexts = original_hydrated
        server._runtime.logs = original_logs


def test_context_snapshot_and_state_preview_defaults() -> None:
    snapshot = load_context_snapshot("ctx-preview")
    state = load_context_state("ctx-preview")

    assert snapshot["preview"]["enabled"] is False
    assert snapshot["preview"]["state"] == "disabled"
    assert snapshot["preview"]["view_mode"] == "active_event"
    assert snapshot["preview"]["bound_session_id"] == ""
    assert snapshot["preview"]["bound_event_id"] == 0
    assert snapshot["preview"]["display"]["framebuffer_extent"] == {"width": 0, "height": 0}
    assert snapshot["preview"]["display"]["viewport_rect"] is None
    assert snapshot["preview"]["display"]["fit_mode"] == "fit_with_screen_cap"
    assert state["preview"]["enabled"] is False
    assert state["preview"]["state"] == "disabled"
    assert state["preview"]["view_mode"] == "active_event"
    assert state["preview"]["display"]["screen_cap_ratio"] == 0.5


def test_get_context_exposes_preview_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_ready(context_id: str):  # type: ignore[no-untyped-def]
        return server.server_runtime._context_state(context_id)

    monkeypatch.setattr(server.server_runtime, "ensure_context_ready", _fake_ready)

    payload = asyncio.run(
        server.dispatch_operation(
            "rd.session.get_context",
            {"context_id": "ctx-preview"},
            transport="test",
        )
    )

    assert payload["ok"] is True
    assert payload["data"]["preview"]["enabled"] is False
    assert payload["data"]["preview"]["state"] == "disabled"
    assert payload["data"]["preview"]["view_mode"] == "active_event"
    assert payload["data"]["preview"]["display"]["window_rect"] == {"width": 0, "height": 0}


def test_open_preview_rejects_non_current_session(monkeypatch: pytest.MonkeyPatch) -> None:
    save_context_state(
        {
            "context_id": "ctx-preview",
            "current_capture_file_id": "capf_preview",
            "current_session_id": "sess_current",
            "captures": {
                "capf_preview": {
                    "capture_file_id": "capf_preview",
                    "file_path": "C:/captures/preview.rdc",
                    "read_only": True,
                }
            },
            "sessions": {
                "sess_current": {
                    "session_id": "sess_current",
                    "capture_file_id": "capf_preview",
                    "rdc_path": "C:/captures/preview.rdc",
                    "frame_index": 0,
                    "active_event_id": 7,
                    "backend_type": "local",
                    "state": "active",
                    "is_live": True,
                }
            },
        },
        "ctx-preview",
    )

    async def _fake_ready(context_id: str):  # type: ignore[no-untyped-def]
        return server.server_runtime._context_state(context_id)

    monkeypatch.setattr(server.server_runtime, "ensure_context_ready", _fake_ready)

    payload = asyncio.run(
        server.dispatch_operation(
            "rd.session.open_preview",
            {"context_id": "ctx-preview", "session_id": "sess_other"},
            transport="test",
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "preview_session_mismatch"


def test_open_preview_failure_does_not_leave_enabled_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    save_context_state(
        {
            "context_id": "ctx-preview",
            "current_capture_file_id": "capf_preview",
            "current_session_id": "sess_current",
            "captures": {
                "capf_preview": {
                    "capture_file_id": "capf_preview",
                    "file_path": "C:/captures/preview.rdc",
                    "read_only": True,
                }
            },
            "sessions": {
                "sess_current": {
                    "session_id": "sess_current",
                    "capture_file_id": "capf_preview",
                    "rdc_path": "C:/captures/preview.rdc",
                    "frame_index": 0,
                    "active_event_id": 23,
                    "backend_type": "local",
                    "state": "active",
                    "is_live": True,
                }
            },
        },
        "ctx-preview",
    )

    async def _fake_ready(context_id: str):  # type: ignore[no-untyped-def]
        return server.server_runtime._context_state(context_id)

    async def _fake_ensure_live_session(session_id: str):  # type: ignore[no-untyped-def]
        return server.ReplayHandle(session_id=str(session_id), capture_file_id="capf_preview", frame_index=0, active_event_id=23)

    async def _fake_get_controller(session_id: str):  # type: ignore[no-untyped-def]
        return SimpleNamespace()

    async def _fake_load_action_index(session_id: str, *, controller=None):  # type: ignore[no-untyped-def]
        action = SimpleNamespace(eventId=23, flags=0, children=[], customName="draw", name="draw")
        return [action], [action], {23: action}

    async def _fake_create_preview_binding(context_id: str, *, title: str):  # type: ignore[no-untyped-def]
        raise server.server_runtime._preview_error(
            "preview_window_create_failed",
            "Failed to create preview window: test",
            context_id=context_id,
        )

    monkeypatch.setattr(server.server_runtime, "ensure_context_ready", _fake_ready)
    monkeypatch.setattr(server.server_runtime, "_ensure_live_session", _fake_ensure_live_session)
    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_load_action_index", _fake_load_action_index)
    monkeypatch.setattr(server.server_runtime, "_create_preview_binding", _fake_create_preview_binding)

    payload = asyncio.run(
        server.dispatch_operation(
            "rd.session.open_preview",
            {"context_id": "ctx-preview"},
            transport="test",
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "preview_window_create_failed"
    state = load_context_state("ctx-preview")
    assert state["preview"]["enabled"] is False
    assert state["preview"]["state"] == "disabled"
    assert state["preview"]["last_error"]


def test_close_preview_clears_enabled_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    save_context_state(
        {
            "context_id": "ctx-preview",
            "current_capture_file_id": "capf_preview",
            "current_session_id": "sess_current",
            "preview": {
                "enabled": True,
                "state": "live",
                "view_mode": "active_event",
                "bound_session_id": "sess_current",
                "bound_capture_file_id": "capf_preview",
                "bound_event_id": 11,
                "backend": "local",
                "recovered_from_session_id": "",
                "rebind_count": 0,
                "last_error": "",
            },
            "captures": {
                "capf_preview": {
                    "capture_file_id": "capf_preview",
                    "file_path": "C:/captures/preview.rdc",
                    "read_only": True,
                }
            },
            "sessions": {
                "sess_current": {
                    "session_id": "sess_current",
                    "capture_file_id": "capf_preview",
                    "rdc_path": "C:/captures/preview.rdc",
                    "frame_index": 0,
                    "active_event_id": 11,
                    "backend_type": "local",
                    "state": "active",
                    "is_live": True,
                }
            },
        },
        "ctx-preview",
    )

    async def _fake_ready(context_id: str):  # type: ignore[no-untyped-def]
        return server.server_runtime._context_state(context_id)

    async def _fake_close_preview_binding(context_id=None, *, close_window=True):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(server.server_runtime, "ensure_context_ready", _fake_ready)
    monkeypatch.setattr(server.server_runtime, "_close_preview_binding", _fake_close_preview_binding)

    payload = asyncio.run(
        server.dispatch_operation(
            "rd.session.close_preview",
            {"context_id": "ctx-preview"},
            transport="test",
        )
    )

    assert payload["ok"] is True
    assert payload["data"]["preview"]["enabled"] is False
    assert payload["data"]["preview"]["state"] == "disabled"


def test_open_preview_respects_runtime_owner_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    save_context_state(
        {
            "context_id": "ctx-preview",
            "current_capture_file_id": "capf_preview",
            "current_session_id": "sess_current",
            "captures": {
                "capf_preview": {
                    "capture_file_id": "capf_preview",
                    "file_path": "C:/captures/preview.rdc",
                    "read_only": True,
                }
            },
            "sessions": {
                "sess_current": {
                    "session_id": "sess_current",
                    "capture_file_id": "capf_preview",
                    "rdc_path": "C:/captures/preview.rdc",
                    "frame_index": 0,
                    "active_event_id": 23,
                    "backend_type": "local",
                    "state": "active",
                    "is_live": True,
                }
            },
        },
        "ctx-preview",
    )

    async def _fake_ready(context_id: str):  # type: ignore[no-untyped-def]
        return server.server_runtime._context_state(context_id)

    async def _fake_sync_preview(context_id=None, *, strict=False, enable_intent=None):  # type: ignore[no-untyped-def]
        ctx = server.server_runtime.normalize_context_id(context_id or "ctx-preview")
        state = server.server_runtime._context_state(ctx)
        preview = server.server_runtime._preview_update_payload(
            ctx,
            server.server_runtime._preview_state_value(ctx),
            enabled=True,
            state_name="live",
            bound_session_id=str(state.get("current_session_id") or ""),
            bound_capture_file_id=str(state.get("current_capture_file_id") or ""),
            bound_event_id=23,
            backend="local",
            last_error="",
            display=server.server_runtime._preview_display_state(
                output_slot=0,
                texture_id="ResourceId::55",
                texture_format="R8G8B8A8_UNORM",
                framebuffer_extent={"width": 1280, "height": 720},
                viewport_rect={"x": 10, "y": 20, "width": 640, "height": 360},
                scissor_rect={"x": 20, "y": 30, "width": 320, "height": 180},
                effective_region_rect={"x": 20, "y": 30, "width": 320, "height": 180},
                region_marker_mode="viewport_scissor_overlay",
                window_rect={"width": 640, "height": 360},
            ),
        )
        return server.server_runtime._store_preview_state(ctx, preview)

    monkeypatch.setattr(server.server_runtime, "ensure_context_ready", _fake_ready)
    monkeypatch.setattr(server.server_runtime, "_sync_context_preview", _fake_sync_preview)

    claimed = asyncio.run(
        server.dispatch_operation(
            "rd.session.claim_runtime_owner",
            {
                "runtime_owner": "rdc-debugger",
                "context_id": "ctx-preview",
                "entry_mode": "cli",
                "backend": "local",
            },
            transport="test",
        )
    )
    assert claimed["ok"] is True
    lease_id = claimed["data"]["owner_lease"]["lease_id"]

    blocked = asyncio.run(
        server.dispatch_operation(
            "rd.session.open_preview",
            {"context_id": "ctx-preview"},
            transport="test",
        )
    )
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "runtime_owner_conflict"

    allowed = asyncio.run(
        server.dispatch_operation(
            "rd.session.open_preview",
            {
                "context_id": "ctx-preview",
                "runtime_owner": "rdc-debugger",
                "owner_lease_id": lease_id,
            },
            transport="test",
        )
    )
    assert allowed["ok"] is True
    assert allowed["data"]["preview"]["enabled"] is True
    assert allowed["data"]["preview"]["state"] == "live"
    assert allowed["data"]["preview"]["display"]["output_slot"] == 0


def test_choose_visual_output_target_uses_event_binding_texture_when_outputs_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_output_targets(session_id: str, event_id: int | None):  # type: ignore[no-untyped-def]
        return []

    async def _inline_offload(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        return fn(*args, **kwargs)

    async def _fake_get_controller(session_id: str):  # type: ignore[no-untyped-def]
        return _FakeController()

    class _FakeController:
        def GetTextures(self):  # type: ignore[no-untyped-def]
            return [
                SimpleNamespace(
                    resourceId="ResourceId::777",
                    width=1920,
                    height=1080,
                    format=SimpleNamespace(Name=lambda: "R8G8B8A8_UNORM"),
                    name="SceneColor",
                )
            ]

    class _FakePipelineService:
        async def get_resource_bindings(self, session_id: str, event_id: int, session_manager: object):  # type: ignore[no-untyped-def]
            return [
                SimpleNamespace(
                    resource_id="ResourceId::777",
                    resource_name="SceneColorOutput",
                    type="UAV",
                    binding=3,
                )
            ]

    async def _fake_get_texture_stats(*, session_id: str, event_id: int, texture_id: object, session_manager: object):  # type: ignore[no-untyped-def]
        return {
            "channels": {
                "r": {"min": 0.0, "max": 1.0},
                "g": {"min": 0.0, "max": 1.0},
                "b": {"min": 0.0, "max": 1.0},
                "a": {"min": 1.0, "max": 1.0},
            },
            "has_any_nan": False,
            "has_any_inf": False,
        }

    monkeypatch.setattr(server.server_runtime, "_output_target_resource_ids", _fake_output_targets)
    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_pipeline_service", _FakePipelineService())
    monkeypatch.setattr(server.server_runtime, "_render_service", SimpleNamespace(get_texture_stats=_fake_get_texture_stats))

    texture_id, texture_desc, output_slot, target_source = asyncio.run(
        server.server_runtime._choose_visual_output_target(
            "sess-preview",
            66,
            allow_framebuffer_fallback=False,
        )
    )

    assert str(texture_id) == "ResourceId::777"
    assert texture_desc is not None
    assert output_slot is None
    assert target_source == "event_binding_uav_3"


def test_choose_visual_output_target_honors_requested_rt_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_output_targets(session_id: str, event_id: int | None):  # type: ignore[no-untyped-def]
        return [("ResourceId::3410", 0), ("ResourceId::3409", 1)]

    async def _fake_get_texture_descriptor(session_id: str, texture_id: object, *, event_id: int | None = None):  # type: ignore[no-untyped-def]
        return (
            texture_id,
            SimpleNamespace(
                width=128,
                height=128,
                name=f"tex_{texture_id}",
                format=SimpleNamespace(Name=lambda: "R8G8B8A8_UNORM"),
            ),
        )

    monkeypatch.setattr(server.server_runtime, "_output_target_resource_ids", _fake_output_targets)
    monkeypatch.setattr(server.server_runtime, "_get_texture_descriptor", _fake_get_texture_descriptor)

    texture_id, texture_desc, output_slot, target_source = asyncio.run(
        server.server_runtime._choose_visual_output_target(
            "sess-preview",
            1847,
            target={"rt_index": 0},
            allow_framebuffer_fallback=False,
        )
    )

    assert str(texture_id) == "ResourceId::3410"
    assert texture_desc is not None
    assert output_slot == 0
    assert target_source == "event_output_slot"


def test_choose_visual_output_target_defaults_to_rt0_before_visual_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_output_targets(session_id: str, event_id: int | None):  # type: ignore[no-untyped-def]
        return [("ResourceId::3410", 0), ("ResourceId::3409", 1)]

    async def _fake_get_texture_descriptor(session_id: str, texture_id: object, *, event_id: int | None = None):  # type: ignore[no-untyped-def]
        return (
            texture_id,
            SimpleNamespace(
                width=128,
                height=128,
                name=f"tex_{texture_id}",
                format=SimpleNamespace(Name=lambda: "R8G8B8A8_UNORM"),
            ),
        )

    async def _fake_get_texture_stats(*, session_id: str, event_id: int, texture_id: object, session_manager: object):  # type: ignore[no-untyped-def]
        if str(texture_id) == "ResourceId::3410":
            spread = 0.1
        else:
            spread = 1.0
        return {
            "channels": {
                "r": {"min": 0.0, "max": spread},
                "g": {"min": 0.0, "max": spread},
                "b": {"min": 0.0, "max": spread},
                "a": {"min": 1.0, "max": 1.0},
            },
            "has_any_nan": False,
            "has_any_inf": False,
        }

    monkeypatch.setattr(server.server_runtime, "_output_target_resource_ids", _fake_output_targets)
    monkeypatch.setattr(server.server_runtime, "_get_texture_descriptor", _fake_get_texture_descriptor)
    monkeypatch.setattr(server.server_runtime, "_render_service", SimpleNamespace(get_texture_stats=_fake_get_texture_stats))

    texture_id, texture_desc, output_slot, target_source = asyncio.run(
        server.server_runtime._choose_visual_output_target(
            "sess-preview",
            1847,
            allow_framebuffer_fallback=False,
        )
    )

    assert str(texture_id) == "ResourceId::3410"
    assert texture_desc is not None
    assert output_slot == 0
    assert target_source == "event_output_slot"


def test_preview_window_fit_helpers_cover_screen_cap_and_centering() -> None:
    assert fit_size_within_bounds(2048, 2048, 960, 540) == (540, 540)
    assert fit_size_within_bounds(1280, 720, 960, 540) == (960, 540)
    assert fit_content_rect(960, 540, 2048, 2048) == {
        "x": 210,
        "y": 0,
        "width": 540,
        "height": 540,
    }


def test_preview_region_rects_clip_and_intersect() -> None:
    viewport_rect, scissor_rect, effective = server.server_runtime._preview_region_rects(
        {"width": 2048, "height": 2048},
        viewport_rect={"x": -10, "y": 0, "width": 1034, "height": 1024},
        scissor_rect={"x": 128, "y": 64, "width": 900, "height": 700},
    )

    assert viewport_rect == {"x": 0, "y": 0, "width": 1024, "height": 1024}
    assert scissor_rect == {"x": 128, "y": 64, "width": 900, "height": 700}
    assert effective == {"x": 128, "y": 64, "width": 896, "height": 700}


def test_export_screenshot_preserves_event_binding_target_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    saved_path = tmp_path / "event_binding_preview.png"
    saved_path.write_bytes(b"png")

    async def _fake_choose_visual_output_target(  # type: ignore[no-untyped-def]
        session_id: str,
        event_id: int,
        *,
        target=None,
        allow_framebuffer_fallback=True,
    ):
        return (
            "ResourceId::777",
            SimpleNamespace(
                width=128,
                height=128,
                name="SceneColor",
                format=SimpleNamespace(Name=lambda: "R8G8B8A8_UNORM"),
            ),
            None,
            "event_binding_uav_3",
        )

    async def _fake_binding_name_index(session_id: str, event_id: int | None):  # type: ignore[no-untyped-def]
        return {}

    async def _fake_dispatch_texture(action: str, args: dict[str, object]):  # type: ignore[no-untyped-def]
        assert action == "render_overlay"
        return json.dumps(
            {
                "success": True,
                "artifact_path": str(saved_path),
                "saved_path": str(saved_path),
                "image_path": str(saved_path),
                "meta": {"width": 128, "height": 128},
                "binding_truth_level": "binding_verified",
                "visual_truth_level": "visual_valid",
                "evidence_truth_level": "visual_evidence_only",
                "summary_degraded_reasons": [],
            }
        )

    monkeypatch.setattr(server.server_runtime, "_choose_visual_output_target", _fake_choose_visual_output_target)
    monkeypatch.setattr(server.server_runtime, "_binding_name_index_for_event", _fake_binding_name_index)
    monkeypatch.setattr(server.server_runtime, "_dispatch_texture", _fake_dispatch_texture)

    payload = json.loads(
        asyncio.run(
            server.server_runtime._dispatch_export(
                "screenshot",
                {
                    "session_id": "sess-preview",
                    "event_id": 103,
                    "file_format": "png",
                    "output_path": str(saved_path),
                },
            )
        )
    )

    assert payload["success"] is True
    assert payload["resolved_event_id"] == 103
    assert payload["texture_id"] == "ResourceId::777"
    assert payload["target_source"] == "event_binding_uav_3"
    assert payload["saved_path"] == str(saved_path)


def test_set_active_waits_for_preview_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    server._runtime.replays = {
        "sess_preview": server.ReplayHandle(
            session_id="sess_preview",
            capture_file_id="capf_preview",
            frame_index=0,
            active_event_id=11,
        )
    }
    server.server_runtime._set_context_runtime_session(
        "sess_preview",
        capture_file_id="capf_preview",
        backend_type="local",
        frame_index=0,
        active_event_id=11,
    )

    class _FakeController:
        def __init__(self) -> None:
            self.calls: list[tuple[int, bool]] = []

        def SetFrameEvent(self, event_id: int, apply: bool) -> None:  # type: ignore[no-untyped-def]
            self.calls.append((int(event_id), bool(apply)))

    fake_controller = _FakeController()

    async def _fake_load_action_index(session_id: str, *, controller=None):  # type: ignore[no-untyped-def]
        action = SimpleNamespace(eventId=77, flags=0, children=[], customName="draw", name="draw")
        return [action], [action], {77: action}

    async def _fake_get_controller(session_id: str):  # type: ignore[no-untyped-def]
        return fake_controller

    async def _inline_offload(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        return fn(*args, **kwargs)

    preview_sync_calls: list[str] = []

    async def _fake_preview_sync(context_id=None):  # type: ignore[no-untyped-def]
        preview_sync_calls.append(str(context_id or "default"))

    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_load_action_index", _fake_load_action_index)
    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_auto_sync_preview_if_enabled", _fake_preview_sync)

    payload = asyncio.run(
        server.dispatch_operation(
            "rd.event.set_active",
            {"session_id": "sess_preview", "event_id": 77},
            transport="test",
        )
    )

    assert payload["ok"] is True
    assert fake_controller.calls == [(77, True)]
    assert preview_sync_calls == ["default"]


def test_set_frame_waits_for_preview_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    server._runtime.replays = {
        "sess_preview": server.ReplayHandle(
            session_id="sess_preview",
            capture_file_id="capf_preview",
            frame_index=0,
            active_event_id=11,
        )
    }

    class _FakeController:
        def GetRootActions(self):  # type: ignore[no-untyped-def]
            return [SimpleNamespace(eventId=11, flags=0, children=[], customName="draw", name="draw")]

    async def _fake_get_controller(session_id: str):  # type: ignore[no-untyped-def]
        return _FakeController()

    async def _inline_offload(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        return fn(*args, **kwargs)

    async def _fake_pick_previewable_default_event_id(session_id: str, actions, *, fallback_event_id=0):  # type: ignore[no-untyped-def]
        return 11

    preview_sync_calls: list[str] = []

    async def _fake_preview_sync(context_id=None):  # type: ignore[no-untyped-def]
        preview_sync_calls.append(str(context_id or "default"))

    async def _fake_ensure_live_session(session_id: str):  # type: ignore[no-untyped-def]
        return server._runtime.replays[str(session_id)]

    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_pick_previewable_default_event_id", _fake_pick_previewable_default_event_id)
    monkeypatch.setattr(server.server_runtime, "_auto_sync_preview_if_enabled", _fake_preview_sync)
    monkeypatch.setattr(server.server_runtime, "_ensure_live_session", _fake_ensure_live_session)

    payload = asyncio.run(
        server.dispatch_operation(
            "rd.replay.set_frame",
            {"session_id": "sess_preview", "frame_index": 0},
            transport="test",
        )
    )

    assert payload["ok"] is True
    assert payload["data"]["active_event_id"] == 11
    assert preview_sync_calls == ["default"]


def test_select_session_waits_for_preview_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    capture_a.write_text("a", encoding="utf-8")
    capture_b.write_text("b", encoding="utf-8")

    server._runtime.captures = {
        "capf_a": server.CaptureFileHandle(capture_file_id="capf_a", file_path=str(capture_a), read_only=True),
        "capf_b": server.CaptureFileHandle(capture_file_id="capf_b", file_path=str(capture_b), read_only=True),
    }
    server._runtime.replays = {
        "sess_a": server.ReplayHandle(session_id="sess_a", capture_file_id="capf_a", frame_index=0, active_event_id=11),
        "sess_b": server.ReplayHandle(session_id="sess_b", capture_file_id="capf_b", frame_index=0, active_event_id=22),
    }
    server.server_runtime._set_context_runtime_session("sess_a", capture_file_id="capf_a", backend_type="local", frame_index=0, active_event_id=11)
    server.server_runtime._set_context_runtime_session("sess_b", capture_file_id="capf_b", backend_type="local", frame_index=0, active_event_id=22)

    preview_sync_calls: list[str] = []

    async def _fake_preview_sync(context_id=None):  # type: ignore[no-untyped-def]
        preview_sync_calls.append(str(context_id or "default"))

    monkeypatch.setattr(server.server_runtime, "_auto_sync_preview_if_enabled", _fake_preview_sync)

    payload = asyncio.run(
        server.dispatch_operation(
            "rd.session.select_session",
            {"session_id": "sess_a"},
            transport="test",
        )
    )

    assert payload["ok"] is True
    assert payload["data"]["current_session_id"] == "sess_a"
    assert preview_sync_calls == ["default"]


def test_resume_waits_for_preview_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    save_context_state(
        {
            "context_id": "default",
            "current_capture_file_id": "capf_preview",
            "current_session_id": "sess_preview",
            "captures": {
                "capf_preview": {
                    "capture_file_id": "capf_preview",
                    "file_path": "C:/captures/preview.rdc",
                    "read_only": True,
                }
            },
            "sessions": {
                "sess_preview": {
                    "session_id": "sess_preview",
                    "capture_file_id": "capf_preview",
                    "rdc_path": "C:/captures/preview.rdc",
                    "frame_index": 0,
                    "active_event_id": 23,
                    "backend_type": "local",
                    "state": "active",
                    "is_live": True,
                }
            },
        },
        "default",
    )

    async def _fake_recover_context_sessions(context_id: str):  # type: ignore[no-untyped-def]
        return server.server_runtime._context_state(context_id)

    preview_sync_calls: list[str] = []

    async def _fake_preview_sync(context_id=None):  # type: ignore[no-untyped-def]
        preview_sync_calls.append(str(context_id or "default"))

    monkeypatch.setattr(server.server_runtime, "_recover_context_sessions", _fake_recover_context_sessions)
    monkeypatch.setattr(server.server_runtime, "_auto_sync_preview_if_enabled", _fake_preview_sync)

    payload = asyncio.run(
        server.dispatch_operation(
            "rd.session.resume",
            {},
            transport="test",
        )
    )

    assert payload["ok"] is True
    assert payload["data"]["current_session_id"] == "sess_preview"
    assert preview_sync_calls == ["default"]


def test_preview_docs_and_catalog_are_synchronized() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    readme = (repo_root / "README.md").read_text(encoding="utf-8-sig")
    quickstart = (repo_root / "docs" / "quickstart.md").read_text(encoding="utf-8-sig")
    session_model = (repo_root / "docs" / "session-model.md").read_text(encoding="utf-8-sig")
    agent_model = (repo_root / "docs" / "agent-model.md").read_text(encoding="utf-8-sig")
    troubleshooting = (repo_root / "docs" / "troubleshooting.md").read_text(encoding="utf-8-sig")
    tools_doc = (repo_root / "docs" / "tools.md").read_text(encoding="utf-8-sig")
    governance = (repo_root / "docs" / "doc-governance.md").read_text(encoding="utf-8-sig")
    configuration = (repo_root / "docs" / "configuration.md").read_text(encoding="utf-8-sig")
    docs_readme = (repo_root / "docs" / "README.md").read_text(encoding="utf-8-sig")
    android_smoke = (repo_root / "docs" / "android-remote-cli-smoke-prompt.md").read_text(encoding="utf-8-sig")
    scripts_readme = (repo_root / "scripts" / "README.md").read_text(encoding="utf-8-sig")
    agents = (repo_root / "AGENTS.md").read_text(encoding="utf-8-sig")
    catalog = json.loads((repo_root / "spec" / "tool_catalog.json").read_text(encoding="utf-8-sig"))
    tools = {item["name"]: item for item in catalog.get("tools") or []}

    assert "`rd.session.open_preview`" in readme
    assert "`rd.session.get_context.preview`" in readme
    assert "完整 framebuffer" in readme
    assert "session preview on" in quickstart
    assert "preview.display" in quickstart
    assert "preview" in session_model
    assert "viewport / scissor" in session_model
    assert "`rd.session.open_preview`" in agent_model
    assert "preview.display" in agent_model
    assert "preview 打不开或自动失效" in troubleshooting
    assert "preview 看着不全、留黑边或像是畸形" in troubleshooting
    assert "`rd.session.open_preview`" in tools_doc
    assert "preview.display" in tools_doc
    assert "`rd.session.open_preview`" in governance
    assert "preview_geometry_smoke.py" in governance
    assert "preview 运行约束" in configuration
    assert "screen_cap_ratio" in configuration
    assert "preview_geometry_smoke.py" in docs_readme
    assert "preview_geometry_smoke.py" in android_smoke
    assert "preview_geometry_smoke.py" in scripts_readme
    assert "preview / 几何观察面改动" in agents
    assert tools["rd.session.open_preview"]["group"].startswith("3.17")
    assert "preview" in tools["rd.session.get_context"]["returns_raw"]
    assert "framebuffer_extent" in tools["rd.session.get_context"]["returns_raw"]

    frameworks_root = repo_root.parent / "RDC-Agent-Frameworks" / "debugger"
    if frameworks_root.is_dir():
        debugger_readme = (frameworks_root / "README.md").read_text(encoding="utf-8-sig")
        agent_core = (frameworks_root / "common" / "AGENT_CORE.md").read_text(encoding="utf-8-sig")
        cli_mode = (frameworks_root / "common" / "docs" / "cli-mode-reference.md").read_text(encoding="utf-8-sig")
        runtime_coordination = (frameworks_root / "common" / "docs" / "runtime-coordination-model.md").read_text(encoding="utf-8-sig")
        truth_store = (frameworks_root / "common" / "docs" / "truth_store_contract.md").read_text(encoding="utf-8-sig")

        assert "preview" in debugger_readme
        assert "preview" in agent_core
        assert "session preview on|off|status" in cli_mode
        assert "human observer" in runtime_coordination
        assert "preview" in truth_store
