from __future__ import annotations

import argparse
import asyncio

from rdx import cli as rdx_cli


def test_daemon_status_uses_loaded_state_before_cleanup(monkeypatch) -> None:
    captured: list[dict] = []
    cleanup_calls: list[str] = []
    loaded_state = {"pid": 123, "token": "tok", "pipe_name": "pipe-1", "context_id": "ctx-status"}

    monkeypatch.setattr(rdx_cli, "load_daemon_state", lambda context="default": dict(loaded_state) if context == "ctx-status" else {})
    monkeypatch.setattr(rdx_cli, "cleanup_stale_daemon_states", lambda context=None: cleanup_calls.append(str(context or "")) or {"state_files": [], "session_files": [], "killed_pids": []})

    def _fake_daemon_request(method: str, *, params=None, timeout=0.0, state=None, context="default"):
        assert method == "status"
        assert context == "ctx-status"
        assert state == loaded_state
        return {"ok": True, "result": {"running": True, "state": {**loaded_state, "last_activity_at": "2026-03-07T00:00:00+00:00"}}}

    monkeypatch.setattr(rdx_cli, "daemon_request", _fake_daemon_request)
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    args = argparse.Namespace(command="daemon", daemon_cmd="status", daemon_context="ctx-status")
    exit_code = asyncio.run(rdx_cli._main_async(args))

    assert exit_code == rdx_cli.EXIT_OK
    assert cleanup_calls == []
    assert captured[0]["ok"] is True
    assert captured[0]["data"]["state"]["pid"] == 123
