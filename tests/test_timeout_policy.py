from __future__ import annotations

import asyncio
import json

from rdx import cli, server
from rdx.timeout_policy import (
    DAEMON_RESPONSE_BUFFER_S,
    DEFAULT_DAEMON_REQUEST_TIMEOUT_S,
    HEAVY_DAEMON_REQUEST_TIMEOUT_S,
    LOCAL_OPEN_FILE_TIMEOUT_S,
    LOCAL_OPEN_REPLAY_TIMEOUT_S,
    REMOTE_CONNECT_DAEMON_BUFFER_S,
    REMOTE_CONNECT_DEFAULT_TIMEOUT_MS,
    REMOTE_OPEN_REPLAY_TIMEOUT_S,
    SESSION_CONTEXT_TIMEOUT_S,
    daemon_exec_timeout_s,
    worker_exec_timeout_s,
)


class DummyRemoteServer:
    def Ping(self):
        return type("PingStatus", (), {"OK": lambda self: True, "Message": lambda self: "Succeeded"})()

    def DriverName(self):
        return "Android Vulkan"

    def RemoteSupportedReplays(self):
        return ["Vulkan"]

    def ShutdownConnection(self):
        return None


def test_daemon_exec_timeout_uses_heavy_window_for_event_queries() -> None:
    assert daemon_exec_timeout_s("rd.event.get_actions", {"session_id": "sess_demo"}) == (
        HEAVY_DAEMON_REQUEST_TIMEOUT_S + DAEMON_RESPONSE_BUFFER_S
    )


def test_daemon_exec_timeout_uses_remote_connect_timeout_ms() -> None:
    timeout_s = daemon_exec_timeout_s("rd.remote.connect", {"host": "127.0.0.1", "timeout_ms": 123456})
    assert timeout_s == 124 + REMOTE_CONNECT_DAEMON_BUFFER_S


def test_daemon_exec_timeout_uses_long_window_for_remote_open_replay() -> None:
    timeout_s = daemon_exec_timeout_s(
        "rd.capture.open_replay",
        {"capture_file_id": "capf_demo", "options": {"remote_id": "remote_demo"}},
    )
    assert timeout_s == REMOTE_OPEN_REPLAY_TIMEOUT_S + DAEMON_RESPONSE_BUFFER_S


def test_daemon_exec_timeout_uses_medium_window_for_local_open_replay() -> None:
    timeout_s = daemon_exec_timeout_s(
        "rd.capture.open_replay",
        {"capture_file_id": "capf_demo", "options": {}},
    )
    assert timeout_s == LOCAL_OPEN_REPLAY_TIMEOUT_S + DAEMON_RESPONSE_BUFFER_S


def test_daemon_exec_timeout_uses_open_file_window() -> None:
    assert daemon_exec_timeout_s("rd.capture.open_file", {"file_path": "demo.rdc"}) == (
        LOCAL_OPEN_FILE_TIMEOUT_S + DAEMON_RESPONSE_BUFFER_S
    )
    assert worker_exec_timeout_s("rd.capture.open_file", {"file_path": "demo.rdc"}) == LOCAL_OPEN_FILE_TIMEOUT_S


def test_daemon_exec_timeout_uses_session_context_window() -> None:
    assert daemon_exec_timeout_s("rd.session.get_context", {}) == (
        SESSION_CONTEXT_TIMEOUT_S + DAEMON_RESPONSE_BUFFER_S
    )


def test_worker_exec_timeout_uses_operation_window_without_transport_buffer() -> None:
    assert worker_exec_timeout_s("rd.event.get_actions", {"session_id": "sess_demo"}) == HEAVY_DAEMON_REQUEST_TIMEOUT_S
    assert worker_exec_timeout_s("rd.core.get_version", {}) == DEFAULT_DAEMON_REQUEST_TIMEOUT_S


def test_cli_daemon_exec_passes_policy_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_daemon_request(method: str, *, params=None, timeout=0.0, state=None, context="default"):
        captured["method"] = method
        captured["params"] = params
        captured["timeout"] = timeout
        captured["context"] = context
        return {"ok": True, "result": {"ok": True}}

    monkeypatch.setattr(cli, "daemon_request", _fake_daemon_request)
    monkeypatch.setattr(cli, "_ensure_daemon_state", lambda context: {"context_id": context})

    payload = cli._daemon_exec(
        "rd.remote.connect",
        {"host": "127.0.0.1", "timeout_ms": 120000},
        context="ctx-demo",
    )

    assert payload == {"ok": True}
    assert captured["method"] == "exec"
    assert captured["timeout"] == 120 + REMOTE_CONNECT_DAEMON_BUFFER_S
    assert captured["context"] == "ctx-demo"


def test_dispatch_remote_connect_uses_default_timeout_when_missing(monkeypatch) -> None:
    original_remotes = dict(server._runtime.remotes)
    original_enable_remote = server._runtime.enable_remote
    captured: dict[str, object] = {}

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def _capture_wait(url: str, timeout_ms: int) -> None:
        captured["url"] = url
        captured["timeout_ms"] = timeout_ms

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_wait_for_remote_endpoint", _capture_wait)
    monkeypatch.setattr(server.server_runtime, "_create_remote_server_connection", lambda url: DummyRemoteServer())

    server._runtime.remotes.clear()
    server._runtime.enable_remote = True
    try:
        payload = json.loads(asyncio.run(server._dispatch_remote("connect", {"host": "127.0.0.1", "port": 38920})))
        assert payload["success"] is True
        assert captured["url"] == "127.0.0.1:38920"
        assert captured["timeout_ms"] == REMOTE_CONNECT_DEFAULT_TIMEOUT_MS
    finally:
        server._runtime.remotes.clear()
        server._runtime.remotes.update(original_remotes)
        server._runtime.enable_remote = original_enable_remote
