from __future__ import annotations

import json
from pathlib import Path

from rdx.daemon import client as daemon_client


def _configure_runtime_dir(monkeypatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "runtime"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(daemon_client, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon_client, "DAEMON_STATE_FILE", state_dir / "daemon_state.json")
    monkeypatch.setattr(daemon_client, "SESSION_STATE_FILE", state_dir / "session_state.json")


def _configure_context_artifact_paths(monkeypatch, tmp_path: Path) -> Path:
    state_dir = tmp_path / "runtime"
    state_dir.mkdir(parents=True, exist_ok=True)

    def _ctx_path(prefix: str, suffix: str, context: str = "default") -> Path:
        ctx = daemon_client._normalize_context(context)
        if ctx == "default":
            return state_dir / f"{prefix}{suffix}"
        return state_dir / f"{prefix}_{daemon_client._sanitize_context(ctx)}{suffix}"

    monkeypatch.setattr(daemon_client, "context_state_path", lambda context="default": _ctx_path("runtime_state", ".json", context))
    monkeypatch.setattr(daemon_client, "context_snapshot_path", lambda context="default": _ctx_path("context_snapshot", ".json", context))
    monkeypatch.setattr(daemon_client, "context_log_path", lambda context="default": _ctx_path("runtime_logs", ".jsonl", context))
    monkeypatch.setattr(daemon_client, "worker_state_path", lambda context="default": _ctx_path("worker_state", ".json", context))
    return state_dir


def test_session_state_isolated_by_context(monkeypatch, tmp_path: Path) -> None:
    _configure_runtime_dir(monkeypatch, tmp_path)

    daemon_client.save_session_state({"session_id": "sess-default"}, context="default")
    daemon_client.save_session_state({"session_id": "sess-custom"}, context="demo ctx")

    default_path = daemon_client.session_state_path("default")
    custom_path = daemon_client.session_state_path("demo ctx")

    assert default_path.name == "session_state.json"
    assert custom_path.name == "session_state_demo_ctx.json"
    assert daemon_client.load_session_state(context="default")["session_id"] == "sess-default"
    assert daemon_client.load_session_state(context="demo ctx")["session_id"] == "sess-custom"


def test_cleanup_stale_daemon_state_removes_dead_pid(monkeypatch, tmp_path: Path) -> None:
    _configure_runtime_dir(monkeypatch, tmp_path)

    state_path = daemon_client.STATE_DIR / "daemon_state_dead.json"
    state_path.write_text(
        json.dumps(
            {
                "context_id": "dead",
                "daemon_context": "dead",
                "pipe_name": "pipe-dead",
                "token": "token-dead",
                "pid": 99999,
                "started_at": "2026-03-06T00:00:00+00:00",
                "last_activity_at": "2026-03-06T00:00:00+00:00",
                "owner_pid": 0,
                "lease_timeout_seconds": 120,
                "idle_timeout_seconds": 900,
                "attached_clients": [],
            },
        ),
        encoding="utf-8",
    )
    daemon_client.save_session_state({"session_id": "stale"}, context="dead")

    monkeypatch.setattr(daemon_client, "_is_process_running", lambda pid: False)

    cleaned = daemon_client.cleanup_stale_daemon_states()

    assert "daemon_state_dead.json" in cleaned["state_files"]
    assert "session_state_dead.json" in cleaned["session_files"]
    assert not state_path.exists()
    assert not daemon_client.session_state_path("dead").exists()


def test_clear_context_without_daemon_clears_session(monkeypatch, tmp_path: Path) -> None:
    _configure_runtime_dir(monkeypatch, tmp_path)

    daemon_client.save_session_state({"session_id": "sess-x"}, context="ctx-x")
    ok, message, details = daemon_client.clear_context("ctx-x")

    assert ok is True
    assert "context cleared" in message
    assert details["state"] == {}
    assert not daemon_client.session_state_path("ctx-x").exists()



def test_cleanup_stale_daemon_state_skips_invalid_json(monkeypatch, tmp_path: Path) -> None:
    _configure_runtime_dir(monkeypatch, tmp_path)

    state_path = daemon_client.STATE_DIR / "daemon_state_busy.json"
    state_path.write_text('{"context_id":', encoding="utf-8")

    cleaned = daemon_client.cleanup_stale_daemon_states()

    assert cleaned["state_files"] == []
    assert cleaned["session_files"] == []
    assert state_path.exists()


def test_cleanup_stale_daemon_states_clears_orphan_context_artifacts(monkeypatch, tmp_path: Path) -> None:
    _configure_runtime_dir(monkeypatch, tmp_path)
    state_dir = _configure_context_artifact_paths(monkeypatch, tmp_path)

    orphan_paths = [
        state_dir / "runtime_state_orphan.json",
        state_dir / "context_snapshot_orphan.json",
        state_dir / "runtime_logs_orphan.jsonl",
        state_dir / "session_state_orphan.json",
        state_dir / "worker_state_orphan.json",
    ]
    for path in orphan_paths:
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(daemon_client, "list_context_ids", lambda: ["orphan"])

    cleaned = daemon_client.cleanup_stale_daemon_states()

    assert "runtime_state_orphan.json" in cleaned["context_files"]
    assert "context_snapshot_orphan.json" in cleaned["snapshot_files"]
    assert "runtime_logs_orphan.jsonl" in cleaned["log_files"]
    assert "session_state_orphan.json" in cleaned["session_files"]
    assert "worker_state_orphan.json" in cleaned["worker_files"]
    assert all(not path.exists() for path in orphan_paths)


def test_save_daemon_state_uses_atomic_replace(monkeypatch, tmp_path: Path) -> None:
    _configure_runtime_dir(monkeypatch, tmp_path)

    calls: list[tuple[str, str]] = []
    real_replace = daemon_client.os.replace

    def _spy_replace(src: str | Path, dst: str | Path) -> None:
        calls.append((Path(src).name, Path(dst).name))
        real_replace(src, dst)

    monkeypatch.setattr(daemon_client.os, "replace", _spy_replace)

    daemon_client.save_daemon_state({"pid": 42, "token": "tok", "pipe_name": "pipe", "context_id": "ctx"}, context="ctx")

    state = daemon_client.load_daemon_state(context="ctx")
    assert state["pid"] == 42
    assert calls
    assert not list(daemon_client.STATE_DIR.glob("*.tmp"))


def test_stop_daemon_uses_loaded_state_before_cleanup(monkeypatch, tmp_path: Path) -> None:
    _configure_runtime_dir(monkeypatch, tmp_path)

    calls: list[tuple[int, str]] = []
    cleared: list[str] = []
    state = {"pid": 321, "context_id": "ctx-live", "token": "tok", "pipe_name": "pipe-live"}

    monkeypatch.setattr(daemon_client, "load_daemon_state", lambda context="default": dict(state) if context == "ctx-live" else {})
    monkeypatch.setattr(daemon_client, "cleanup_stale_daemon_states", lambda context=None: (_ for _ in ()).throw(AssertionError("cleanup should not run before stop uses loaded state")))
    monkeypatch.setattr(daemon_client, "_shutdown_stateful_daemon", lambda pid, loaded, context: calls.append((pid, context)))
    monkeypatch.setattr(daemon_client, "_is_process_running", lambda pid: False)
    monkeypatch.setattr(daemon_client, "clear_daemon_state", lambda context="default": cleared.append(f"daemon:{context}"))
    monkeypatch.setattr(daemon_client, "clear_session_state", lambda context="default": cleared.append(f"session:{context}"))

    ok, message = daemon_client.stop_daemon("ctx-live")

    assert ok is True
    assert message == "daemon stopped"
    assert calls == [(321, "ctx-live")]
    assert cleared == ["daemon:ctx-live", "session:ctx-live"]


def test_daemon_request_timeout_returns_structured_details(monkeypatch, tmp_path: Path) -> None:
    _configure_runtime_dir(monkeypatch, tmp_path)

    class _FakeClient:
        def __init__(self, *, address: str, family: str) -> None:
            self.address = address
            self.family = family

        def send(self, payload: dict[str, object]) -> None:
            return None

        def poll(self, timeout: float) -> bool:
            return False

        def close(self) -> None:
            return None

    state = {
        "pipe_name": "pipe-demo",
        "token": "tok-demo",
        "context_id": "ctx-demo",
        "active_operation": {"operation": "rd.capture.open_replay", "trace_id": "trace-1", "transport": "cli"},
        "pid": 123,
        "session_id": "sess-demo",
        "capture_file_id": "capf-demo",
        "active_request_count": 1,
    }

    monkeypatch.setattr(daemon_client, "Client", _FakeClient)

    try:
        daemon_client.daemon_request(
            "exec",
            params={"operation": "rd.texture.get_data"},
            timeout=0.01,
            state=state,
            context="ctx-demo",
        )
    except daemon_client.DaemonRequestTimeout as exc:
        assert exc.code == "daemon_timeout"
        assert exc.details["operation"] == "rd.texture.get_data"
        assert exc.details["context_id"] == "ctx-demo"
        assert exc.details["timeout_seconds"] == 0.01
        assert exc.details["active_operation"]["operation"] == "rd.capture.open_replay"
        assert exc.details["daemon_state_excerpt"]["session_id"] == "sess-demo"
    else:  # pragma: no cover
        raise AssertionError("expected DaemonRequestTimeout")
