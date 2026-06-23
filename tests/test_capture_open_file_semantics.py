from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from rdx import server
from rdx.context_snapshot import clear_context_snapshot
from rdx.runtime_state import clear_context_state


class _FakeCaptureFile:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def OpenFile(self, path: str, *_args) -> str:
        self.calls.append(f"OpenFile:{path}")
        return "ok"

    def DriverName(self) -> str:
        self.calls.append("DriverName")
        return "D3D12"

    def OpenCapture(self, *_args):
        raise AssertionError("rd.capture.open_file must not call OpenCapture")

    def CloseFile(self) -> None:
        self.calls.append("CloseFile")


def test_dispatch_capture_open_file_does_not_open_replay(monkeypatch, tmp_path) -> None:
    original_captures = dict(server._runtime.captures)
    fake_capture = _FakeCaptureFile()
    sample = tmp_path / "sample.rdc"
    sample.write_bytes(b"rdc")

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_check_status", lambda status, operation, **kwargs: None)
    monkeypatch.setattr(server.server_runtime, "_get_rd", lambda: SimpleNamespace(OpenCaptureFile=lambda: fake_capture))

    clear_context_snapshot()
    clear_context_state()
    server._runtime.context_snapshots.clear()
    server._runtime.context_states.clear()
    server._runtime.hydrated_contexts.clear()
    server._runtime.captures.clear()
    try:
        payload = json.loads(asyncio.run(server._dispatch_capture("open_file", {"file_path": str(sample)})))
        assert payload["success"] is True
        assert payload["driver"] == "D3D12"
        assert fake_capture.calls == [f"OpenFile:{sample}", "DriverName", "CloseFile"]
    finally:
        clear_context_snapshot()
        clear_context_state()
        server._runtime.context_snapshots.clear()
        server._runtime.context_states.clear()
        server._runtime.hydrated_contexts.clear()
        server._runtime.captures.clear()
        server._runtime.captures.update(original_captures)


def test_dispatch_capture_open_replay_returns_after_ready_state_without_preview_sync(monkeypatch, tmp_path) -> None:
    original_captures = dict(server._runtime.captures)
    original_replays = dict(server._runtime.replays)
    original_session_manager = server.server_runtime._session_manager
    sample = tmp_path / "sample.rdc"
    sample.write_bytes(b"rdc")

    class _FakeController:
        def GetRootActions(self):  # noqa: N802
            return []

        def GetAPIProperties(self):  # noqa: N802
            raise AssertionError("open_replay should not wait for API properties after ready state")

    class _FakeSessionManager:
        def __init__(self) -> None:
            self.controller = _FakeController()

        async def create_session(self, *, backend_config, replay_config):  # type: ignore[no-untyped-def]
            return SimpleNamespace(session_id="sess_demo")

        async def open_capture(self, session_id: str, rdc_path: str):  # type: ignore[no-untyped-def]
            assert session_id == "sess_demo"
            assert rdc_path == str(sample)
            return SimpleNamespace(frame_count=1)

        def get_controller(self, session_id: str):  # type: ignore[no-untyped-def]
            assert session_id == "sess_demo"
            return self.controller

        async def close_session(self, session_id: str) -> None:
            raise AssertionError("open_replay success should not close the session")

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _pick_previewable_default_event_id(*_args, **_kwargs) -> int:
        return 147

    async def _unexpected_preview_sync(*_args, **_kwargs) -> None:
        raise AssertionError("open_replay should not auto-sync preview before returning")

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_pick_default_event_id", lambda _roots: 147)
    monkeypatch.setattr(server.server_runtime, "_pick_previewable_default_event_id", _pick_previewable_default_event_id)
    monkeypatch.setattr(server.server_runtime, "_auto_sync_preview_if_enabled", _unexpected_preview_sync)

    clear_context_snapshot()
    clear_context_state()
    server._runtime.context_snapshots.clear()
    server._runtime.context_states.clear()
    server._runtime.hydrated_contexts.clear()
    server._runtime.captures.clear()
    server._runtime.replays.clear()
    server._runtime.captures["capf_demo"] = server.server_runtime.CaptureFileHandle(
        capture_file_id="capf_demo",
        file_path=str(sample),
        read_only=True,
        driver="D3D12",
    )
    server.server_runtime._session_manager = _FakeSessionManager()
    try:
        payload = json.loads(
            asyncio.run(server._dispatch_capture("open_replay", {"capture_file_id": "capf_demo", "options": {}}))
        )

        assert payload["success"] is True
        assert payload["session_id"] == "sess_demo"
        assert payload["capture_file_id"] == "capf_demo"
        assert payload["active_event_id"] == 147
        assert payload["recovery_status"] == "ready"
        assert payload["api_properties"] == {}
    finally:
        clear_context_snapshot()
        clear_context_state()
        server._runtime.context_snapshots.clear()
        server._runtime.context_states.clear()
        server._runtime.hydrated_contexts.clear()
        server._runtime.captures.clear()
        server._runtime.captures.update(original_captures)
        server._runtime.replays.clear()
        server._runtime.replays.update(original_replays)
        server.server_runtime._session_manager = original_session_manager

def test_dispatch_capture_open_replay_reuses_live_session_for_same_capture(monkeypatch, tmp_path) -> None:
    original_captures = dict(server._runtime.captures)
    original_replays = dict(server._runtime.replays)
    original_session_manager = server.server_runtime._session_manager
    sample = tmp_path / "sample.rdc"
    sample.write_bytes(b"rdc")

    class _UnexpectedSessionManager:
        async def create_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("live same-capture replay should be reused")

        async def open_capture(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("live same-capture replay should be reused")

    clear_context_snapshot()
    clear_context_state()
    server._runtime.context_snapshots.clear()
    server._runtime.context_states.clear()
    server._runtime.hydrated_contexts.clear()
    server._runtime.captures.clear()
    server._runtime.replays.clear()
    server._runtime.captures["capf_demo"] = server.server_runtime.CaptureFileHandle(
        capture_file_id="capf_demo",
        file_path=str(sample),
        read_only=True,
        driver="D3D12",
    )
    server._runtime.replays["sess_live"] = server.server_runtime.ReplayHandle(
        session_id="sess_live",
        capture_file_id="capf_demo",
        frame_index=0,
        active_event_id=7010,
    )
    server.server_runtime._session_manager = _UnexpectedSessionManager()
    try:
        payload = json.loads(
            asyncio.run(server._dispatch_capture("open_replay", {"capture_file_id": "capf_demo", "options": {}}))
        )

        assert payload["success"] is True
        assert payload["session_id"] == "sess_live"
        assert payload["capture_file_id"] == "capf_demo"
        assert payload["active_event_id"] == 7010
        assert payload["reused_session"] is True
    finally:
        clear_context_snapshot()
        clear_context_state()
        server._runtime.context_snapshots.clear()
        server._runtime.context_states.clear()
        server._runtime.hydrated_contexts.clear()
        server._runtime.captures.clear()
        server._runtime.captures.update(original_captures)
        server._runtime.replays.clear()
        server._runtime.replays.update(original_replays)
        server.server_runtime._session_manager = original_session_manager