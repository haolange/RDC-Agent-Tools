from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from rdx import server
from rdx.core.session_manager import _map_graphics_api
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


class FailingExecuteStatus:
    def OK(self) -> bool:
        return False

    def Message(self) -> str:
        return "Couldn't connect to target program"


class FailingExecuteResult:
    def __init__(self) -> None:
        self.result = FailingExecuteStatus()
        self.ident = 0


class LaunchFailRemoteServer(DummyRemoteServer):
    def ExecuteAndInject(self, app: str, working_dir: str, cmdline: str, env: list[object], opts: object) -> FailingExecuteResult:
        return FailingExecuteResult()


class FakeController:
    def GetRootActions(self) -> list[SimpleNamespace]:
        return []

    def GetAPIProperties(self) -> SimpleNamespace:
        return SimpleNamespace(pipelineType="Vulkan")


class FakeSessionManager:
    def __init__(self) -> None:
        self.backend_config: dict[str, object] | None = None
        self.closed: list[str] = []
        self.controller = FakeController()

    async def create_session(self, *, backend_config: dict[str, object], replay_config: dict[str, object]) -> SimpleNamespace:
        self.backend_config = dict(backend_config)
        return SimpleNamespace(session_id="sess_demo")

    async def open_capture(self, session_id: str, path: str) -> SimpleNamespace:
        return SimpleNamespace(frame_count=2)

    async def close_session(self, session_id: str) -> None:
        self.closed.append(session_id)

    def get_controller(self, session_id: str) -> FakeController:
        return self.controller


class FakeMeshFormat:
    type = 1
    compCount = 4
    compByteWidth = 4
    compType = "Float"
    special = False
    bgraOrder = False
    srgbCorrected = False


class FakeMesh:
    def __init__(self, *, vertex_resource_id: object = "ResourceId::123", stride: int = 16) -> None:
        self.vertexResourceId = vertex_resource_id
        self.vertexByteOffset = 0
        self.vertexByteSize = stride * 2
        self.vertexByteStride = stride
        self.indexResourceId = "ResourceId::0"
        self.indexByteOffset = 0
        self.indexByteSize = 0
        self.indexByteStride = 0
        self.numIndices = 2
        self.topology = "TriangleList"
        self.status = ""
        self.format = FakeMeshFormat()


class FakeMeshController:
    def __init__(self, mesh: FakeMesh) -> None:
        self.mesh = mesh

    def GetPostVSData(self, instance: int, view_index: int, stage: object) -> FakeMesh:
        return self.mesh

    def GetBufferData(self, resource_id: object, offset: int, size: int) -> bytes:
        return bytes.fromhex("0000803f000000400000404000008040") * 2


class FakeMeshOutput:
    def GetShader(self, stage: object) -> None:
        return None


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


def test_dispatch_capture_open_replay_keeps_live_remote_handle(monkeypatch) -> None:
    original_session_manager = server.server_runtime._session_manager
    original_captures = dict(server._runtime.captures)
    original_replays = dict(server._runtime.replays)
    original_remotes = dict(server._runtime.remotes)
    original_session_owned = dict(server._runtime.session_owned_remotes)
    original_consumed = dict(server._runtime.consumed_remotes)
    fake_manager = FakeSessionManager()

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(
        server.server_runtime,
        "_get_controller",
        lambda session_id: (_ for _ in ()).throw(AssertionError("_dispatch_capture.open_replay should use SessionManager.get_controller directly")),
    )

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
        assert server._runtime.remotes["remote_demo"].transport == "adb_android"
        assert server._runtime.remotes["remote_demo"].leased_session_ids == ["sess_demo"]
        assert server._runtime.session_owned_remotes["sess_demo"].transport == "adb_android"
        assert "remote_demo" not in server._runtime.consumed_remotes

        context_payload = json.loads(asyncio.run(server._dispatch_session("get_context", {})))
        assert context_payload["success"] is True
        assert context_payload["runtime"]["session_id"] == "sess_demo"
        assert context_payload["remote"]["state"] == "live_handle"
        assert context_payload["remote"]["origin_remote_id"] == "remote_demo"
        assert context_payload["remote"]["active_session_ids"] == ["sess_demo"]
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


def test_session_manager_maps_numeric_graphics_api_values() -> None:
    assert _map_graphics_api(SimpleNamespace(pipelineType=3)).value == "Vulkan"


def test_dispatch_remote_launch_app_surfaces_execute_and_inject_status(monkeypatch) -> None:
    original_remotes = dict(server._runtime.remotes)
    original_enable_remote = server._runtime.enable_remote

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)

    server._runtime.remotes = {
        "remote_demo": server.RemoteHandle(
            remote_id="remote_demo",
            host="127.0.0.1",
            port=38960,
            connected=True,
            transport="adb_android",
            remote_server=LaunchFailRemoteServer(),
        )
    }
    server._runtime.enable_remote = True
    try:
        payload = json.loads(
            asyncio.run(
                server._dispatch_remote(
                    "launch_app",
                    {"remote_id": "remote_demo", "exe_path": "com.android.settings", "working_dir": "", "cmdline": "", "env": {}, "capture_options": {}},
                )
            )
        )
        assert payload["success"] is False
        assert payload["code"] == "remote_launch_failed"
        assert "Couldn't connect to target program" in payload["error_message"]
    finally:
        server._runtime.remotes = original_remotes
        server._runtime.enable_remote = original_enable_remote


def test_dispatch_mesh_post_gs_returns_empty_payload_when_stage_not_bound(monkeypatch) -> None:
    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _fake_get_controller(session_id: str) -> FakeMeshController:
        return FakeMeshController(FakeMesh(vertex_resource_id=None))

    async def _fake_get_output(session_id: str) -> FakeMeshOutput:
        return FakeMeshOutput()

    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        return int(event_id or 0)

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_get_output", _fake_get_output)
    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_rd_stage", lambda stage: stage)
    monkeypatch.setattr(server.server_runtime, "_get_rd", lambda: SimpleNamespace(MeshDataStage=SimpleNamespace(VSOut="vs", GSOut="gs")))

    payload = json.loads(
        asyncio.run(
            server._dispatch_mesh(
                "get_post_gs_data",
                {"session_id": "sess_demo", "event_id": 314, "instance": 0, "max_primitives": 64},
            )
        )
    )

    assert payload["success"] is True
    mesh = payload["mesh_data"]
    assert mesh["stage"] == "GS"
    assert mesh["stage_bound"] is False
    assert mesh["vertex_count"] == 0


def test_dispatch_mesh_post_vs_serializes_format_payload(monkeypatch) -> None:
    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _fake_get_controller(session_id: str) -> FakeMeshController:
        return FakeMeshController(FakeMesh())

    async def _fake_ensure_event(session_id: str, event_id: int | None) -> int:
        return int(event_id or 0)

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    monkeypatch.setattr(server.server_runtime, "_ensure_event", _fake_ensure_event)
    monkeypatch.setattr(server.server_runtime, "_get_rd", lambda: SimpleNamespace(MeshDataStage=SimpleNamespace(VSOut="vs", GSOut="gs")))

    payload = json.loads(
        asyncio.run(
            server._dispatch_mesh(
                "get_post_vs_data",
                {"session_id": "sess_demo", "event_id": 314, "view": "vs_out", "instance": 0, "view_index": 0, "max_vertices": 8},
            )
        )
    )

    assert payload["success"] is True
    mesh = payload["mesh_data"]
    assert mesh["vertex_count"] == 2
    assert mesh["mesh_format"]["format"]["compCount"] == 4
    assert mesh["mesh_format"]["format"]["compByteWidth"] == 4
