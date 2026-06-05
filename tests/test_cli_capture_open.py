from __future__ import annotations

import argparse
import asyncio

from rdx import cli as rdx_cli


def test_capture_open_success_returns_machine_readable_payload(monkeypatch, tmp_path) -> None:
    captured: list[dict] = []
    seen_ops: list[str] = []
    capture_path = tmp_path / "sample.rdc"
    capture_path.write_text("rdc", encoding="utf-8")

    def _fake_daemon_exec(operation: str, args: dict[str, object], *, remote: bool = False, context: str = "default"):  # type: ignore[no-untyped-def]
        assert context == "ctx-demo"
        seen_ops.append(operation)
        if operation == "rd.core.init":
            return {"ok": True, "data": {}}
        if operation == "rd.capture.open_file":
            return {"ok": True, "data": {"capture_file_id": "capf_demo"}}
        if operation == "rd.capture.open_replay":
            return {
                "ok": True,
                "data": {
                    "session_id": "sess_demo",
                    "capture_file_id": "capf_demo",
                    "active_event_id": 147,
                    "recovery_status": "ready",
                },
            }
        if operation == "rd.replay.set_frame":
            return {"ok": True, "data": {"active_event_id": 6152}}
        if operation == "rd.session.get_context":
            return {
                "ok": True,
                "data": {
                    "context_id": "ctx-demo",
                    "current_session_id": "sess_demo",
                    "runtime": {
                        "session_id": "sess_demo",
                        "capture_file_id": "capf_demo",
                        "frame_index": 0,
                        "active_event_id": 6152,
                        "backend_type": "local",
                    },
                    "sessions": [
                        {
                            "session_id": "sess_demo",
                            "capture_file_id": "capf_demo",
                            "recovery": {"status": "ready"},
                        }
                    ],
                },
            }
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(rdx_cli, "_daemon_exec", _fake_daemon_exec)
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    args = argparse.Namespace(
        command="capture",
        capture_cmd="open",
        file=str(capture_path),
        frame_index=0,
        artifact_dir=str(tmp_path / "artifacts"),
        daemon_context="ctx-demo",
        preview=False,
    )
    exit_code = asyncio.run(rdx_cli._cmd_capture_open(args))

    assert exit_code == rdx_cli.EXIT_OK
    assert seen_ops == [
        "rd.core.init",
        "rd.capture.open_file",
        "rd.capture.open_replay",
        "rd.replay.set_frame",
        "rd.session.get_context",
    ]
    assert captured[0]["ok"] is True
    assert captured[0]["result_kind"] == "rdx.capture.open"
    assert captured[0]["data"]["context_id"] == "ctx-demo"
    assert captured[0]["data"]["capture_file_id"] == "capf_demo"
    assert captured[0]["data"]["capture_path"] == str(capture_path.resolve())
    assert captured[0]["data"]["session_id"] == "sess_demo"
    assert captured[0]["data"]["active_event_id"] == 6152
    assert captured[0]["data"]["recovery_status"] == "ready"
    assert captured[0]["data"]["runtime"]["session_id"] == "sess_demo"
    assert captured[0]["data"]["context"]["current_session_id"] == "sess_demo"


def test_capture_open_wraps_open_replay_failure_with_step_state(monkeypatch, tmp_path) -> None:
    captured: list[dict] = []
    capture_path = tmp_path / "sample.rdc"
    capture_path.write_text("rdc", encoding="utf-8")

    def _fake_daemon_exec(operation: str, args: dict[str, object], *, remote: bool = False, context: str = "default"):  # type: ignore[no-untyped-def]
        assert context == "ctx-demo"
        if operation == "rd.core.init":
            return {"ok": True, "data": {}}
        if operation == "rd.capture.open_file":
            return {"ok": True, "data": {"capture_file_id": "capf_demo"}}
        if operation == "rd.capture.open_replay":
            return {
                "ok": False,
                "error": {
                    "code": "renderdoc_error",
                    "category": "runtime",
                    "message": "remote.OpenCapture failed",
                    "details": {"stage": "OpenCapture"},
                },
            }
        if operation == "rd.session.get_context":
            return {
                "ok": True,
                "data": {
                    "context_id": "ctx-demo",
                    "runtime": {
                        "session_id": "",
                        "capture_file_id": "capf_demo",
                        "frame_index": 0,
                        "active_event_id": 0,
                        "backend_type": "local",
                    },
                },
            }
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(rdx_cli, "_daemon_exec", _fake_daemon_exec)
    monkeypatch.setattr(
        rdx_cli,
        "_daemon_status_payload",
        lambda context: {
            "ok": True,
            "data": {
                "running": True,
                "state": {
                    "context_id": context,
                    "session_id": "",
                    "capture_file_id": "capf_demo",
                    "active_request_count": 1,
                    "runtime_owner": {"agent_id": "", "lease_id": "", "status": "unclaimed"},
                },
            },
        },
    )
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    args = argparse.Namespace(
        command="capture",
        capture_cmd="open",
        file=str(capture_path),
        frame_index=0,
        artifact_dir=str(tmp_path / "artifacts"),
        daemon_context="ctx-demo",
    )
    exit_code = asyncio.run(rdx_cli._cmd_capture_open(args))

    assert exit_code == rdx_cli.EXIT_RUNTIME_ERR
    assert captured[0]["ok"] is False
    assert captured[0]["error"]["code"] == "renderdoc_error"
    assert captured[0]["error"]["details"]["failed_step"] == "open_replay"
    assert captured[0]["error"]["details"]["capture_file_id"] == "capf_demo"
    assert captured[0]["error"]["details"]["daemon_state"]["context_id"] == "ctx-demo"
    assert captured[0]["error"]["details"]["context_snapshot"]["ok"] is True


def test_capture_open_wraps_open_replay_exception_with_step_state(monkeypatch, tmp_path) -> None:
    captured: list[dict] = []
    capture_path = tmp_path / "sample.rdc"
    capture_path.write_text("rdc", encoding="utf-8")

    def _fake_daemon_exec(operation: str, args: dict[str, object], *, remote: bool = False, context: str = "default"):  # type: ignore[no-untyped-def]
        assert context == "ctx-demo"
        if operation == "rd.core.init":
            return {"ok": True, "data": {}}
        if operation == "rd.capture.open_file":
            return {"ok": True, "data": {"capture_file_id": "capf_demo"}}
        if operation == "rd.capture.open_replay":
            raise TimeoutError("daemon timeout")
        if operation == "rd.session.get_context":
            return {
                "ok": True,
                "data": {
                    "context_id": "ctx-demo",
                    "runtime": {
                        "session_id": "sess_ready",
                        "capture_file_id": "capf_demo",
                        "active_event_id": 147,
                        "backend_type": "local",
                    },
                },
            }
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(rdx_cli, "_daemon_exec", _fake_daemon_exec)
    monkeypatch.setattr(
        rdx_cli,
        "_daemon_status_payload",
        lambda context: {
            "ok": True,
            "data": {
                "running": True,
                "state": {
                    "context_id": context,
                    "capture_file_id": "capf_demo",
                    "session_id": "sess_ready",
                    "active_event_id": 147,
                    "recovery_status": "ready",
                    "worker": {"running": True, "pid": 1234},
                },
            },
        },
    )
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    args = argparse.Namespace(
        command="capture",
        capture_cmd="open",
        file=str(capture_path),
        frame_index=0,
        artifact_dir=str(tmp_path / "artifacts"),
        daemon_context="ctx-demo",
        preview=False,
    )
    exit_code = asyncio.run(rdx_cli._cmd_capture_open(args))

    assert exit_code == rdx_cli.EXIT_RUNTIME_ERR
    assert captured[0]["ok"] is False
    assert captured[0]["error"]["details"]["failed_step"] == "open_replay"
    assert captured[0]["error"]["details"]["capture_file_id"] == "capf_demo"
    assert captured[0]["error"]["details"]["daemon_state"]["session_id"] == "sess_ready"
    assert captured[0]["error"]["details"]["daemon_state"]["worker"]["pid"] == 1234
    assert captured[0]["error"]["details"]["context_snapshot"]["ok"] is True


def test_capture_open_wraps_get_context_exception_with_step_state(monkeypatch, tmp_path) -> None:
    captured: list[dict] = []
    capture_path = tmp_path / "sample.rdc"
    capture_path.write_text("rdc", encoding="utf-8")

    def _fake_daemon_exec(operation: str, args: dict[str, object], *, remote: bool = False, context: str = "default"):  # type: ignore[no-untyped-def]
        assert context == "ctx-demo"
        if operation == "rd.core.init":
            return {"ok": True, "data": {}}
        if operation == "rd.capture.open_file":
            return {"ok": True, "data": {"capture_file_id": "capf_demo"}}
        if operation == "rd.capture.open_replay":
            return {"ok": True, "data": {"session_id": "sess_demo"}}
        if operation == "rd.replay.set_frame":
            return {"ok": True, "data": {"active_event_id": 6152}}
        if operation == "rd.session.get_context":
            raise RuntimeError("daemon timeout")
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(rdx_cli, "_daemon_exec", _fake_daemon_exec)
    monkeypatch.setattr(
        rdx_cli,
        "_daemon_status_payload",
        lambda context: {
            "ok": True,
            "data": {
                "running": True,
                "state": {
                    "context_id": context,
                    "session_id": "sess_demo",
                    "capture_file_id": "capf_demo",
                    "active_request_count": 1,
                    "runtime_owner": {"agent_id": "rdc-debugger", "lease_id": "lease_demo", "status": "claimed"},
                },
            },
        },
    )
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    args = argparse.Namespace(
        command="capture",
        capture_cmd="open",
        file=str(capture_path),
        frame_index=0,
        artifact_dir=str(tmp_path / "artifacts"),
        daemon_context="ctx-demo",
    )
    exit_code = asyncio.run(rdx_cli._cmd_capture_open(args))

    assert exit_code == rdx_cli.EXIT_RUNTIME_ERR
    assert captured[0]["ok"] is False
    assert captured[0]["error"]["details"]["failed_step"] == "get_context"
    assert captured[0]["error"]["details"]["session_id"] == "sess_demo"
    assert captured[0]["error"]["details"]["daemon_state"]["runtime_owner"]["agent_id"] == "rdc-debugger"
    assert captured[0]["error"]["details"]["context_snapshot"]["ok"] is False


def test_capture_open_with_preview_invokes_session_open_preview(monkeypatch, tmp_path) -> None:
    captured: list[dict] = []
    seen_ops: list[str] = []
    capture_path = tmp_path / "sample.rdc"
    capture_path.write_text("rdc", encoding="utf-8")

    def _fake_daemon_exec(operation: str, args: dict[str, object], *, remote: bool = False, context: str = "default"):  # type: ignore[no-untyped-def]
        assert context == "ctx-demo"
        seen_ops.append(operation)
        if operation == "rd.core.init":
            return {"ok": True, "data": {}}
        if operation == "rd.capture.open_file":
            return {"ok": True, "data": {"capture_file_id": "capf_demo"}}
        if operation == "rd.capture.open_replay":
            return {"ok": True, "data": {"session_id": "sess_demo"}}
        if operation == "rd.replay.set_frame":
            return {"ok": True, "data": {"active_event_id": 6152}}
        if operation == "rd.session.open_preview":
            return {"ok": True, "data": {"preview": {"enabled": True, "state": "live"}}}
        if operation == "rd.session.get_context":
            return {
                "ok": True,
                "data": {
                    "context_id": "ctx-demo",
                    "runtime": {
                        "session_id": "sess_demo",
                        "capture_file_id": "capf_demo",
                        "frame_index": 0,
                        "active_event_id": 6152,
                        "backend_type": "local",
                    },
                    "preview": {
                        "enabled": True,
                        "state": "live",
                        "view_mode": "active_event",
                    },
                },
            }
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(rdx_cli, "_daemon_exec", _fake_daemon_exec)
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    args = argparse.Namespace(
        command="capture",
        capture_cmd="open",
        file=str(capture_path),
        frame_index=0,
        artifact_dir=str(tmp_path / "artifacts"),
        daemon_context="ctx-demo",
        preview=True,
    )
    exit_code = asyncio.run(rdx_cli._cmd_capture_open(args))

    assert exit_code == rdx_cli.EXIT_OK
    assert "rd.session.open_preview" in seen_ops
    assert captured[0]["ok"] is True
    assert captured[0]["data"]["context"]["preview"]["enabled"] is True


def test_session_preview_status_reads_context_preview(monkeypatch) -> None:
    captured: list[dict] = []

    monkeypatch.setattr(
        rdx_cli,
        "_daemon_exec",
        lambda operation, args, *, remote=False, context="default": {
            "ok": True,
            "data": {
                "context_id": context,
                "current_session_id": "sess_demo",
                "runtime": {
                    "session_id": "sess_demo",
                    "capture_file_id": "capf_demo",
                    "frame_index": 0,
                    "active_event_id": 6152,
                    "backend_type": "local",
                },
                "preview": {
                    "enabled": True,
                    "state": "live",
                    "view_mode": "active_event",
                    "bound_session_id": "sess_demo",
                    "bound_event_id": 6152,
                },
            },
        },
    )
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    args = argparse.Namespace(
        command="session",
        session_cmd="preview",
        session_preview_cmd="status",
        daemon_context="ctx-demo",
    )
    exit_code = asyncio.run(rdx_cli._cmd_session_preview(args))

    assert exit_code == rdx_cli.EXIT_OK
    assert captured[0]["ok"] is True
    assert captured[0]["data"]["preview"]["enabled"] is True
    assert captured[0]["data"]["preview"]["state"] == "live"
