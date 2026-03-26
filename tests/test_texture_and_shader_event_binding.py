from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rdx import server


class _FakeShaderPipe:
    def GetShader(self, stage: object) -> str:
        if stage == "ps":
            return "ResourceId::77"
        return "ResourceId::0"

    def GetShaderReflection(self, stage: object) -> SimpleNamespace:
        return SimpleNamespace(
            entryPoint="main",
            readOnlyResources=[],
            readWriteResources=[],
            constantBlocks=[],
        )

    def GetGraphicsPipelineObject(self) -> str:
        return "GraphicsPipe"

    def GetComputePipelineObject(self) -> str:
        return "ComputePipe"


class _FakeShaderController:
    def GetPipelineState(self) -> _FakeShaderPipe:
        return _FakeShaderPipe()

    def GetDisassemblyTargets(self, with_pipeline: bool) -> list[str]:
        return ["mock-target"]

    def DisassembleShader(self, pipeline_obj: object, reflection: object, target: str) -> str:
        return f"disassembly:{target}"


@pytest.fixture(autouse=True)
def _restore_runtime_services() -> None:
    original_render_service = server.server_runtime._render_service
    original_session_manager = server.server_runtime._session_manager
    try:
        yield
    finally:
        server.server_runtime._render_service = original_render_service
        server.server_runtime._session_manager = original_session_manager


def test_texture_event_bound_tools_respect_explicit_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_events: list[int] = []

    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        resolved = int(event_id or 0)
        seen_events.append(resolved)
        return resolved

    async def _fake_resolve_texture_id(session_id: str, texture_id: object, *, event_id: int | None = None) -> str:
        return str(texture_id)

    async def _fake_pick_pixel(
        *,
        session_id: str,
        event_id: int,
        texture_id: str,
        x: int,
        y: int,
        session_manager: object,
    ) -> dict[str, object]:
        return {"event_id": int(event_id), "texture_id": texture_id, "x": int(x), "y": int(y), "r": 1.0}

    async def _fake_readback_texture(
        *,
        session_id: str,
        event_id: int,
        texture_id: str,
        session_manager: object,
        artifact_store: object,
        subresource: dict[str, int] | None,
        region: dict[str, int] | None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        return SimpleNamespace(sha256="deadbeef"), {"event_id": int(event_id), "texture_id": texture_id, "shape": [1, 1, 4]}

    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_resolve_texture_id", _fake_resolve_texture_id)
    monkeypatch.setattr(server.server_runtime, "_artifact_path", lambda artifact_ref: "C:/fake/artifact.npz")
    server.server_runtime._session_manager = SimpleNamespace()
    server.server_runtime._render_service = SimpleNamespace(
        pick_pixel=_fake_pick_pixel,
        readback_texture=_fake_readback_texture,
    )

    pixel_payload = json.loads(
        asyncio.run(
            server._dispatch_texture(
                "get_pixel_value",
                {
                    "session_id": "sess_demo",
                    "event_id": 314,
                    "texture_id": "ResourceId::178817",
                    "x": 7,
                    "y": 9,
                },
            )
        )
    )
    stats_payload = json.loads(
        asyncio.run(
            server._dispatch_texture(
                "compute_stats",
                {
                    "session_id": "sess_demo",
                    "event_id": 314,
                    "texture_id": "ResourceId::178817",
                },
            )
        )
    )

    assert pixel_payload["success"] is True
    assert pixel_payload["resolved_event_id"] == 314
    assert pixel_payload["pixel"]["event_id"] == 314
    assert stats_payload["success"] is True
    assert stats_payload["resolved_event_id"] == 314
    assert stats_payload["stats"]["event_id"] == 314
    assert seen_events == [314, 314]


def test_shader_reflection_and_disassembly_support_stage_only_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _inline_offload(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        return fn(*args, **kwargs)

    async def _fake_get_controller(session_id: str) -> _FakeShaderController:
        return _FakeShaderController()

    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        return int(event_id or 314)

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_rd_stage", lambda stage: stage)
    monkeypatch.setattr(server.server_runtime, "_is_null_resource_id", lambda rid: str(rid) in {"", "ResourceId::0", "0"})
    monkeypatch.setattr(
        server.server_runtime.PatchEngine,
        "_resolve_source",
        staticmethod(lambda *args, **kwargs: ("disassembly:mock-target", "raw", "mock-target", False)),
    )

    reflection_payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "get_reflection",
                {
                    "session_id": "sess_demo",
                    "event_id": 314,
                    "stage": "ps",
                },
            )
        )
    )
    disassembly_payload = json.loads(
        asyncio.run(
            server._dispatch_shader(
                "get_disassembly",
                {
                    "session_id": "sess_demo",
                    "event_id": 314,
                    "stage": "ps",
                },
            )
        )
    )

    assert reflection_payload["success"] is True
    assert reflection_payload["resolved_event_id"] == 314
    assert reflection_payload["shader_id"] == "ResourceId::77"
    assert reflection_payload["reflection"]["entry_points"] == ["main"]
    assert disassembly_payload["success"] is True
    assert disassembly_payload["resolved_event_id"] == 314
    assert disassembly_payload["shader_id"] == "ResourceId::77"
    assert disassembly_payload["target"] == "mock-target"
    assert disassembly_payload["disassembly"] == "disassembly:mock-target"


def test_texture_get_data_defaults_to_npz_container(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        return int(event_id or 314)

    async def _fake_resolve_texture_id(session_id: str, texture_id: object, *, event_id: int | None = None) -> str:
        return str(texture_id)

    async def _fake_readback_texture(
        *,
        session_id: str,
        event_id: int,
        texture_id: str,
        session_manager: object,
        artifact_store: object,
        subresource: dict[str, int] | None,
        region: dict[str, int] | None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        return SimpleNamespace(sha256="deadbeef", bytes=128), {"event_id": int(event_id), "pixels": 4}

    artifact_path = tmp_path / "readback.npz"
    artifact_path.write_bytes(b"npz-payload")

    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_resolve_texture_id", _fake_resolve_texture_id)
    monkeypatch.setattr(server.server_runtime, "_artifact_path", lambda artifact_ref: str(artifact_path))
    server.server_runtime._session_manager = SimpleNamespace()
    server.server_runtime._render_service = SimpleNamespace(readback_texture=_fake_readback_texture)

    payload = json.loads(
        asyncio.run(
            server._dispatch_texture(
                "get_data",
                {
                    "session_id": "sess_demo",
                    "event_id": 314,
                    "texture_id": "ResourceId::178817",
                },
            )
        )
    )

    assert payload["success"] is True
    assert payload["container_format"] == "npz"
    assert payload["content_kind"] == "texture_readback_container"
    assert payload["artifact_path"].endswith(".npz")
    assert payload["saved_path"].endswith(".npz")


def test_texture_get_data_rejects_non_npz_output_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        return int(event_id or 314)

    async def _fake_resolve_texture_id(session_id: str, texture_id: object, *, event_id: int | None = None) -> str:
        return str(texture_id)

    async def _fake_readback_texture(
        *,
        session_id: str,
        event_id: int,
        texture_id: str,
        session_manager: object,
        artifact_store: object,
        subresource: dict[str, int] | None,
        region: dict[str, int] | None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        return SimpleNamespace(sha256="deadbeef", bytes=128), {"event_id": int(event_id)}

    artifact_path = tmp_path / "readback.npz"
    artifact_path.write_bytes(b"npz-payload")

    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_resolve_texture_id", _fake_resolve_texture_id)
    monkeypatch.setattr(server.server_runtime, "_artifact_path", lambda artifact_ref: str(artifact_path))
    server.server_runtime._session_manager = SimpleNamespace()
    server.server_runtime._render_service = SimpleNamespace(readback_texture=_fake_readback_texture)

    payload = json.loads(
        asyncio.run(
            server._dispatch_texture(
                "get_data",
                {
                    "session_id": "sess_demo",
                    "event_id": 314,
                    "texture_id": "ResourceId::178817",
                    "output_path": str(tmp_path / "preview.png"),
                },
            )
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "texture_output_path_extension_mismatch"
