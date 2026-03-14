from __future__ import annotations

import asyncio
import json

import pytest

from rdx import server


@pytest.mark.unit
def test_vfs_ls_root_lists_top_level_domains() -> None:
    payload = json.loads(asyncio.run(server._dispatch_vfs("ls", {"path": "/"})))

    assert payload["success"] is True
    entries = payload["entries"]
    names = {entry["name"] for entry in entries}
    assert {"context", "artifacts", "draws", "passes", "resources", "textures", "buffers", "pipeline", "shaders", "debug"} <= names


@pytest.mark.unit
def test_vfs_cat_context_uses_context_snapshot(monkeypatch) -> None:
    async def _fake_dispatch(tool_name: str, args: dict[str, object], **_: object) -> dict[str, object]:
        assert tool_name == "rd.session.get_context"
        return {
            "ok": True,
            "data": {
                "context_id": "ctx-demo",
                "runtime": {"session_id": "sess_demo", "active_event_id": 9},
                "focus": {"pixel": {"x": 3, "y": 4}},
            },
            "error": {},
            "meta": {},
        }

    monkeypatch.setattr(server, "dispatch_operation", _fake_dispatch)

    payload = json.loads(asyncio.run(server._dispatch_vfs("cat", {"path": "/context"})))

    assert payload["success"] is True
    assert payload["node"]["data"]["context_id"] == "ctx-demo"
    assert payload["node"]["data"]["runtime"]["session_id"] == "sess_demo"


@pytest.mark.unit
def test_vfs_draw_shader_path_routes_through_pipeline_tools(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake_dispatch(tool_name: str, args: dict[str, object], **_: object) -> dict[str, object]:
        calls.append((tool_name, dict(args)))
        if tool_name == "rd.pipeline.get_state":
            return {
                "ok": True,
                "data": {
                    "pipeline_state": {
                        "shaders": [
                            {"stage": "VS", "entry": "vsMain"},
                            {"stage": "PS", "entry": "psMain"},
                        ]
                    }
                },
                "error": {},
                "meta": {},
            }
        if tool_name == "rd.pipeline.get_shader":
            return {
                "ok": True,
                "data": {"shader": {"stage": "PS", "entry": "psMain", "shader_id": "shader-ps"}},
                "error": {},
                "meta": {},
            }
        raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(server, "dispatch_operation", _fake_dispatch)

    payload = json.loads(
        asyncio.run(
            server._dispatch_vfs(
                "cat",
                {"path": "/draws/42/shaders/ps", "session_id": "sess_demo"},
            )
        )
    )

    assert payload["success"] is True
    assert payload["node"]["data"]["shader_id"] == "shader-ps"
    assert calls[0] == ("rd.pipeline.get_state", {"session_id": "sess_demo", "event_id": 42})
    assert calls[1] == ("rd.pipeline.get_shader", {"session_id": "sess_demo", "event_id": 42, "stage": "ps"})
