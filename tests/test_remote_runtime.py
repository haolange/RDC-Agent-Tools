from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from rdx import server
from rdx.context_snapshot import clear_context_snapshot


class DummyRemoteServer:
    def __init__(self) -> None:
        self.shutdown_called = False

    def Ping(self) -> SimpleNamespace:
        return SimpleNamespace(OK=lambda: True, Message=lambda: "Succeeded")

    def DriverName(self) -> str:
        return "Android Vulkan"

    def RemoteSupportedReplays(self) -> list[str]:
        return ["Vulkan"]

    def ShutdownConnection(self) -> None:
        self.shutdown_called = True


class FakeController:
    def GetRootActions(self) -> list[SimpleNamespace]:
        return []

    def GetAPIProperties(self) -> SimpleNamespace:
        return SimpleNamespace(pipelineType="Vulkan")


class FakeSessionManager:
    def __init__(self) -> None:
        self.backend_config: dict[str, object] | None = None
        self.closed: list[str] = []

    async def create_session(self, *, backend_config: dict[str, object], replay_config: dict[str, object]) -> SimpleNamespace:
        self.backend_config = dict(backend_config)
        return SimpleNamespace(session_id="sess_demo")

    async def open_capture(self, session_id: str, path: str) -> SimpleNamespace:
        return SimpleNamespace(frame_count=2)

    async def close_session(self, session_id: str) -> None:
        self.closed.append(session_id)


def test_dispatch_remote_connect_returns_live_handle_and_server_info(monkeypatch) -> None:
    original_remotes = dict(server._runtime.remotes)
    original_enable_remote = server._runtime.enable_remote

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_wait_for_remote_endpoint", lambda url, timeout_ms: None)
    monkeypatch.setattr(server.server_runtime, "_create_remote_server_connection", lambda url: DummyRemoteServer())

    server._runtime.remotes.clear()
    server._runtime.enable_remote = True
    clear_context_snapshot()
    server._runtime.context_snapshots.clear()
    try:
        payload = json.loads(
            asyncio.run(server._dispatch_remote("connect", {"host": "127.0.0.1", "port": 38920, "timeout_ms": 1000}))
        )
        assert payload["success"] is True
        assert payload["detail"]["connected"] is True
        assert payload["server_info"]["capabilities"]["supported_replays"] == ["Vulkan"]
        remote_id = payload["remote_id"]
        assert server._runtime.remotes[remote_id].connected is True
    finally:
        clear_context_snapshot()
        server._runtime.context_snapshots.clear()
        server._runtime.remotes.clear()
        server._runtime.remotes.update(original_remotes)
        server._runtime.enable_remote = original_enable_remote


def test_dispatch_remote_connect_failure_does_not_allocate_remote_id(monkeypatch) -> None:
    original_remotes = dict(server._runtime.remotes)
    original_enable_remote = server._runtime.enable_remote

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_wait_for_remote_endpoint", lambda url, timeout_ms: None)

    def _boom(url: str) -> DummyRemoteServer:
        raise RuntimeError("boom")

    monkeypatch.setattr(server.server_runtime, "_create_remote_server_connection", _boom)

    server._runtime.remotes.clear()
    server._runtime.enable_remote = True
    try:
        payload = json.loads(
            asyncio.run(server._dispatch_remote("connect", {"host": "127.0.0.1", "port": 38920, "timeout_ms": 1000}))
        )
        assert payload["success"] is False
        assert "remote_id" not in payload
        assert server._runtime.remotes == {}
    finally:
        server._runtime.remotes.clear()
        server._runtime.remotes.update(original_remotes)
        server._runtime.enable_remote = original_enable_remote


def test_dispatch_capture_open_replay_consumes_remote_handle(monkeypatch) -> None:
    original_session_manager = server.server_runtime._session_manager
    original_captures = dict(server._runtime.captures)
    original_replays = dict(server._runtime.replays)
    original_remotes = dict(server._runtime.remotes)
    original_session_owned = dict(server._runtime.session_owned_remotes)
    original_consumed = dict(server._runtime.consumed_remotes)
    fake_manager = FakeSessionManager()

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _fake_get_controller(session_id: str) -> FakeController:
        return FakeController()

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)

    server.server_runtime._session_manager = fake_manager
    server._runtime.captures = {
        "capf_demo": server.CaptureFileHandle(capture_file_id="capf_demo", file_path="capture.rdc", read_only=True)
    }
    server._runtime.replays = {}
    server._runtime.remotes = {
        "remote_demo": server.RemoteHandle(
            remote_id="remote_demo",
            host="127.0.0.1",
            port=38960,
            connected=True,
            transport="adb_android",
            remote_server=DummyRemoteServer(),
        )
    }
    server._runtime.session_owned_remotes = {}
    server._runtime.consumed_remotes = {}
    clear_context_snapshot()
    server._runtime.context_snapshots.clear()

    try:
        payload = json.loads(
            asyncio.run(
                server._dispatch_capture(
                    "open_replay",
                    {"capture_file_id": "capf_demo", "options": {"remote_id": "remote_demo"}},
                )
            )
        )
        assert payload["success"] is True
        assert fake_manager.backend_config is not None
        assert fake_manager.backend_config["type"] == "remote"
        assert fake_manager.backend_config["host"] == "127.0.0.1"
        assert fake_manager.backend_config["port"] == 38960
        assert fake_manager.backend_config["remote_id"] == "remote_demo"
        assert isinstance(fake_manager.backend_config["remote_server"], DummyRemoteServer)
        assert "remote_demo" not in server._runtime.remotes
        assert server._runtime.session_owned_remotes["sess_demo"].transport == "adb_android"
        assert server._runtime.consumed_remotes["remote_demo"].consumed_by_session_id == "sess_demo"

        context_payload = json.loads(asyncio.run(server._dispatch_session("get_context", {})))
        assert context_payload["success"] is True
        assert context_payload["runtime"]["session_id"] == "sess_demo"
        assert context_payload["remote"]["state"] == "session_owned"
        assert context_payload["remote"]["origin_remote_id"] == "remote_demo"
    finally:
        clear_context_snapshot()
        server._runtime.context_snapshots.clear()
        server.server_runtime._session_manager = original_session_manager
        server._runtime.captures = original_captures
        server._runtime.replays = original_replays
        server._runtime.remotes = original_remotes
        server._runtime.session_owned_remotes = original_session_owned
        server._runtime.consumed_remotes = original_consumed


def test_consumed_remote_handle_reports_lifecycle_error() -> None:
    original_consumed = dict(server._runtime.consumed_remotes)
    server._runtime.consumed_remotes = {
        "remote_demo": server.ConsumedRemoteHandle(
            remote_id="remote_demo",
            endpoint="127.0.0.1:38960",
            consumed_by_session_id="sess_demo",
        )
    }
    try:
        payload = json.loads(asyncio.run(server._dispatch_remote("ping", {"remote_id": "remote_demo"})))
        assert payload["success"] is False
        assert payload["code"] == "remote_handle_consumed"
        assert payload["details"]["consumed_by_session_id"] == "sess_demo"
    finally:
        server._runtime.consumed_remotes = original_consumed
