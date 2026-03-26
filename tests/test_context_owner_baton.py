from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rdx import server
from rdx.context_snapshot import clear_context_snapshot
from rdx.runtime_state import clear_context_state, save_context_state


@pytest.fixture(autouse=True)
def _reset_runtime_state() -> None:
    original_captures = dict(server._runtime.captures)
    original_replays = dict(server._runtime.replays)
    original_context_snapshots = dict(server._runtime.context_snapshots)
    original_context_states = dict(server._runtime.context_states)
    original_hydrated = set(server._runtime.hydrated_contexts)
    original_logs = list(server._runtime.logs)
    original_session_manager = server.server_runtime._session_manager
    original_bootstrapped = server.server_runtime._runtime_bootstrapped
    original_config = server.server_runtime._config
    for context_id in ("default", "ctx-alpha", "ctx-baton"):
        clear_context_snapshot(context_id)
        clear_context_state(context_id)
    server._runtime.captures.clear()
    server._runtime.replays.clear()
    server._runtime.context_snapshots.clear()
    server._runtime.context_states.clear()
    server._runtime.hydrated_contexts.clear()
    server._runtime.logs.clear()
    try:
        yield
    finally:
        for context_id in ("default", "ctx-alpha", "ctx-baton"):
            clear_context_snapshot(context_id)
            clear_context_state(context_id)
        server._runtime.captures = original_captures
        server._runtime.replays = original_replays
        server._runtime.context_snapshots = original_context_snapshots
        server._runtime.context_states = original_context_states
        server._runtime.hydrated_contexts = original_hydrated
        server._runtime.logs = original_logs
        server.server_runtime._session_manager = original_session_manager
        server.server_runtime._runtime_bootstrapped = original_bootstrapped
        server.server_runtime._config = original_config


def test_context_lifecycle_tools_round_trip() -> None:
    created = asyncio.run(
        server.dispatch_operation(
            "rd.session.create_context",
            {"new_context_id": "ctx-alpha"},
            transport="test",
        )
    )
    assert created["ok"] is True
    assert created["data"]["context_id"] == "ctx-alpha"

    updated = asyncio.run(
        server.dispatch_operation(
            "rd.session.update_context",
            {"key": "notes", "value": "alpha-notes", "context_id": "ctx-alpha"},
            transport="test",
        )
    )
    assert updated["ok"] is True
    assert updated["data"]["notes"] == "alpha-notes"

    default_snapshot = asyncio.run(server.dispatch_operation("rd.session.get_context", {}, transport="test"))
    assert default_snapshot["ok"] is True
    assert default_snapshot["data"]["context_id"] == "default"
    assert default_snapshot["data"]["notes"] == ""
    assert default_snapshot["data"]["runtime_parallelism_ceiling"] == "multi_context_multi_owner"

    listed = asyncio.run(server.dispatch_operation("rd.session.list_contexts", {}, transport="test"))
    assert listed["ok"] is True
    context_ids = {item["context_id"] for item in listed["data"]["contexts"]}
    assert {"default", "ctx-alpha"} <= context_ids
    assert all(item["runtime_parallelism_ceiling"] == "multi_context_multi_owner" for item in listed["data"]["contexts"])

    selected = asyncio.run(
        server.dispatch_operation(
            "rd.session.select_context",
            {"target_context_id": "ctx-alpha"},
            transport="test",
        )
    )
    assert selected["ok"] is True
    assert selected["data"]["selected_context_id"] == "ctx-alpha"
    assert selected["data"]["notes"] == "alpha-notes"

    cleared = asyncio.run(
        server.dispatch_operation(
            "rd.session.clear_context",
            {"target_context_id": "ctx-alpha"},
            transport="test",
        )
    )
    assert cleared["ok"] is True
    assert cleared["data"]["context_id"] == "ctx-alpha"
    assert cleared["data"]["notes"] == ""


def test_claim_runtime_owner_blocks_live_tool_without_matching_lease(tmp_path: Path) -> None:
    capture_path = tmp_path / "owner-check.rdc"
    capture_path.write_text("dummy capture", encoding="utf-8")

    claimed = asyncio.run(
        server.dispatch_operation(
            "rd.session.claim_runtime_owner",
            {"runtime_owner": "rdc-debugger", "entry_mode": "cli", "backend": "local"},
            transport="test",
        )
    )
    assert claimed["ok"] is True
    lease_id = claimed["data"]["owner_lease"]["lease_id"]
    assert lease_id

    blocked = asyncio.run(
        server.dispatch_operation(
            "rd.capture.open_file",
            {"file_path": str(capture_path), "read_only": True},
            transport="test",
        )
    )
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "runtime_owner_conflict"

    allowed = asyncio.run(
        server.dispatch_operation(
            "rd.capture.open_file",
            {
                "file_path": str(capture_path),
                "read_only": True,
                "runtime_owner": "rdc-debugger",
                "owner_lease_id": lease_id,
            },
            transport="test",
        )
    )
    if allowed["ok"] is False:
        assert allowed["error"]["code"] != "runtime_owner_conflict"


def test_runtime_baton_export_and_rehydrate_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    capture_path = tmp_path / "baton.rdc"
    capture_path.write_text("baton capture", encoding="utf-8")
    save_context_state(
        {
            "context_id": "ctx-baton",
            "current_capture_file_id": "capf_baton",
            "current_session_id": "sess_baton",
            "captures": {
                "capf_baton": {
                    "capture_file_id": "capf_baton",
                    "file_path": str(capture_path),
                    "read_only": True,
                }
            },
            "sessions": {
                "sess_baton": {
                    "session_id": "sess_baton",
                    "capture_file_id": "capf_baton",
                    "rdc_path": str(capture_path),
                    "frame_index": 0,
                    "active_event_id": 77,
                    "backend_type": "local",
                    "state": "active",
                    "is_live": True,
                }
            },
        },
        "ctx-baton",
    )

    exported = asyncio.run(
        server.dispatch_operation(
            "rd.session.export_runtime_baton",
            {"task_goal": "confirm hotspot source", "context_id": "ctx-baton"},
            transport="test",
        )
    )
    assert exported["ok"] is True
    baton_id = exported["data"]["baton_id"]
    artifact_path = Path(exported["data"]["artifact_path"])
    assert baton_id
    assert artifact_path.is_file()

    calls: list[tuple[str, str]] = []

    async def _fake_recover(context_id: str, session_id: str, *, trace_id: str = "") -> dict[str, str]:
        calls.append((context_id, session_id))
        return {"session_id": session_id}

    monkeypatch.setattr(server.server_runtime, "_recover_single_session_from_state", _fake_recover)

    rehydrated = asyncio.run(
        server.dispatch_operation(
            "rd.session.rehydrate_runtime_baton",
            {"baton_id": baton_id, "context_id": "ctx-baton"},
            transport="test",
        )
    )
    assert rehydrated["ok"] is True
    assert rehydrated["data"]["active_baton"]["baton_id"] == baton_id
    assert rehydrated["data"]["rehydrate_status"]["status"] == "succeeded"
    assert calls == [("ctx-baton", "sess_baton")]


def test_runtime_mode_truth_declares_runtime_ceiling_only() -> None:
    payload = json.loads((Path(__file__).resolve().parents[1] / "spec" / "runtime_mode_truth.json").read_text(encoding="utf-8"))
    modes = payload["modes"]
    assert modes["local_cli"]["runtime_parallelism_ceiling"] == "multi_context_multi_owner"
    assert modes["local_mcp"]["runtime_parallelism_ceiling"] == "multi_context_multi_owner"
    assert modes["remote_daemon"]["runtime_parallelism_ceiling"] == "single_runtime_owner"
    assert modes["remote_mcp"]["runtime_parallelism_ceiling"] == "single_runtime_owner"
    assert modes["remote_mcp"]["host_coordination_gate"] == "frameworks_platform_matrix_applies"
