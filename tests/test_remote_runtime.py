from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from rdx import server


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

    monkeypatch.setattr(server, "_offload", _inline_offload)
    monkeypatch.setattr(server, "_wait_for_remote_endpoint", lambda url, timeout_ms: None)
    monkeypatch.setattr(server, "_create_remote_server_connection", lambda url: DummyRemoteServer())

    server._runtime.remotes.clear()
    server._runtime.enable_remote = True
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
        server._runtime.remotes.clear()
        server._runtime.remotes.update(original_remotes)
        server._runtime.enable_remote = original_enable_remote


def test_dispatch_remote_connect_failure_does_not_allocate_remote_id(monkeypatch) -> None:
    original_remotes = dict(server._runtime.remotes)
    original_enable_remote = server._runtime.enable_remote

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(server, "_offload", _inline_offload)
    monkeypatch.setattr(server, "_wait_for_remote_endpoint", lambda url, timeout_ms: None)

    def _boom(url: str) -> DummyRemoteServer:
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_create_remote_server_connection", _boom)

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


def test_dispatch_capture_open_replay_passes_remote_endpoint(monkeypatch) -> None:
    original_session_manager = server._session_manager
    original_captures = dict(server._runtime.captures)
    original_replays = dict(server._runtime.replays)
    original_remotes = dict(server._runtime.remotes)
    fake_manager = FakeSessionManager()

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _fake_get_controller(session_id: str) -> FakeController:
        return FakeController()

    monkeypatch.setattr(server, "_offload", _inline_offload)
    monkeypatch.setattr(server, "_get_controller", _fake_get_controller)

    server._session_manager = fake_manager
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
        remote_handle = server._runtime.remotes["remote_demo"]
        assert remote_handle.connected is False
        assert remote_handle.remote_server is None
        assert remote_handle.leased_session_id == "sess_demo"
    finally:
        server._session_manager = original_session_manager
        server._runtime.captures = original_captures
        server._runtime.replays = original_replays
        server._runtime.remotes = original_remotes
