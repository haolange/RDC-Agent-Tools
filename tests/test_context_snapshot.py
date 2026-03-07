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
        update_payload = json.loads(
            asyncio.run(server._dispatch_tool_legacy("rd.session.update_context", {"key": "focus_pixel", "value": "12,34"}))
        )
        assert update_payload["success"] is True
        assert update_payload["focus"]["pixel"] == {"x": 12, "y": 34}

        get_payload = json.loads(asyncio.run(server._dispatch_tool_legacy("rd.session.get_context", {})))
        assert get_payload["success"] is True
        assert get_payload["focus"]["pixel"] == {"x": 12, "y": 34}

        invalid_payload = json.loads(
            asyncio.run(server._dispatch_tool_legacy("rd.session.update_context", {"key": "session_id", "value": "sess_demo"}))
        )
        assert invalid_payload["success"] is False
        assert "runtime-owned" in invalid_payload["error_message"]
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
        get_payload = json.loads(asyncio.run(server._dispatch_tool_legacy("rd.session.get_context", {})))
        artifacts = get_payload["last_artifacts"]
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

    monkeypatch.setattr(server, "_dispatch_debug", _fake_debug)
    try:
        asyncio.run(server._dispatch_tool_legacy("rd.session.update_context", {"key": "focus_pixel", "value": "5,9"}))
        payload = json.loads(asyncio.run(server._dispatch_macro("explain_pixel", {"session_id": "sess_demo"})))
        assert payload["success"] is True
        assert payload["history"][0]["event_id"] == 1
    finally:
        clear_context_snapshot()
        server._runtime.context_snapshots.clear()
