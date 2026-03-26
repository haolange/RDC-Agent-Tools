from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rdx import server
from rdx.context_snapshot import clear_context_snapshot
from rdx.core.engine import ExecutionContext


def test_session_context_update_and_get_round_trip() -> None:
    clear_context_snapshot()
    server._runtime.context_snapshots.clear()
    try:
        update_payload = asyncio.run(
            server.dispatch_operation("rd.session.update_context", {"key": "focus_pixel", "value": "12,34"}, transport="test")
        )
        assert update_payload["ok"] is True
        assert update_payload["data"]["focus"]["pixel"] == {"x": 12, "y": 34}

        get_payload = asyncio.run(server.dispatch_operation("rd.session.get_context", {}, transport="test"))
        assert get_payload["ok"] is True
        assert get_payload["data"]["focus"]["pixel"] == {"x": 12, "y": 34}
        assert get_payload["data"]["runtime_parallelism_ceiling"] == "multi_context_multi_owner"

        invalid_payload = asyncio.run(
            server.dispatch_operation("rd.session.update_context", {"key": "session_id", "value": "sess_demo"}, transport="test")
        )
        assert invalid_payload["ok"] is False
        assert "runtime-owned" in invalid_payload["error"]["message"]
    finally:
        clear_context_snapshot()
        server._runtime.context_snapshots.clear()


def test_postprocess_context_snapshot_tracks_recent_artifacts(tmp_path: Path) -> None:
    clear_context_snapshot()
    server._runtime.context_snapshots.clear()
    artifact_path = tmp_path / "artifact.txt"
    artifact_path.write_text("ok", encoding="utf-8")
    payload = {
        "ok": True,
        "artifacts": [{"path": str(artifact_path), "type": "saved_path"}],
        "meta": {},
    }
    ctx = ExecutionContext(transport="test", remote=False, metadata={"context_id": "default"})
    try:
        server._postprocess_context_snapshot("rd.util.pack_zip", {}, payload, ctx)
        get_payload = asyncio.run(server.dispatch_operation("rd.session.get_context", {}, transport="test"))
        artifacts = get_payload["data"]["last_artifacts"]
        assert artifacts
        assert artifacts[0]["path"] == str(artifact_path)
        assert artifacts[0]["source_tool"] == "rd.util.pack_zip"
    finally:
        clear_context_snapshot()
        server._runtime.context_snapshots.clear()


def test_macro_uses_focus_pixel_from_context(monkeypatch) -> None:
    clear_context_snapshot()
    server._runtime.context_snapshots.clear()

    async def _fake_debug(action: str, args: dict[str, object]) -> str:
        assert action == "pixel_history"
        assert args["x"] == 5
        assert args["y"] == 9
        return json.dumps({"success": True, "history": [{"event_id": 1}]})

    monkeypatch.setattr(server.server_runtime, "_dispatch_debug", _fake_debug)
    try:
        asyncio.run(server.dispatch_operation("rd.session.update_context", {"key": "focus_pixel", "value": "5,9"}, transport="test"))
        payload = json.loads(asyncio.run(server._dispatch_macro("explain_pixel", {"session_id": "sess_demo"})))
        assert payload["success"] is True
        assert payload["history"][0]["event_id"] == 1
    finally:
        clear_context_snapshot()
        server._runtime.context_snapshots.clear()


def test_dispatch_operation_respects_explicit_context_id() -> None:
    clear_context_snapshot('ctx-demo')
    server._runtime.context_snapshots.clear()
    try:
        payload = asyncio.run(
            server.dispatch_operation(
                'rd.session.update_context',
                {'key': 'notes', 'value': 'ctx-demo-note'},
                transport='test',
                context_id='ctx-demo',
            )
        )
        assert payload['ok'] is True
        follow_up = asyncio.run(
            server.dispatch_operation(
                'rd.session.get_context',
                {},
                transport='test',
                context_id='ctx-demo',
            )
        )
        assert follow_up['ok'] is True
        assert follow_up['data']['context_id'] == 'ctx-demo'
        assert follow_up['data']['notes'] == 'ctx-demo-note'
    finally:
        clear_context_snapshot('ctx-demo')
        server._runtime.context_snapshots.clear()
