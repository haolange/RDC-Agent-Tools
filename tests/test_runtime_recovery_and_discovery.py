from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from rdx import server
from rdx.context_snapshot import clear_context_snapshot
from rdx.runtime_state import clear_context_state, save_context_state


class _FakeRecoveryController:
    def __init__(self, event_ids: list[int]) -> None:
        self._event_ids = list(event_ids)
        self.set_calls: list[int] = []

    def GetRootActions(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(eventId=event_id, flags=0, children=[], customName=f"evt_{event_id}", name=f"evt_{event_id}") for event_id in self._event_ids]

    def SetFrameEvent(self, event_id: int, apply: bool) -> None:
        self.set_calls.append(int(event_id))


class _FakeRecoverySessionManager:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.opened: list[tuple[str, str]] = []
        self.closed: list[str] = []

    async def create_session(self, *, backend_config: dict[str, object], replay_config: dict[str, object], preferred_session_id: str | None = None) -> SimpleNamespace:
        session_id = str(preferred_session_id or "sess_recovered")
        self.created.append(session_id)
        return SimpleNamespace(session_id=session_id)

    async def open_capture(self, session_id: str, path: str) -> SimpleNamespace:
        self.opened.append((str(session_id), str(path)))
        return SimpleNamespace(frame_count=1)

    async def close_session(self, session_id: str) -> None:
        self.closed.append(str(session_id))


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
    clear_context_snapshot("default")
    clear_context_state("default")
    server._runtime.captures.clear()
    server._runtime.replays.clear()
    server._runtime.context_snapshots.clear()
    server._runtime.context_states.clear()
    server._runtime.hydrated_contexts.clear()
    server._runtime.logs.clear()
    try:
        yield
    finally:
        clear_context_snapshot("default")
        clear_context_state("default")
        server._runtime.captures = original_captures
        server._runtime.replays = original_replays
        server._runtime.context_snapshots = original_context_snapshots
        server._runtime.context_states = original_context_states
        server._runtime.hydrated_contexts = original_hydrated
        server._runtime.logs = original_logs
        server.server_runtime._session_manager = original_session_manager
        server.server_runtime._runtime_bootstrapped = original_bootstrapped
        server.server_runtime._config = original_config


def test_operation_history_and_runtime_metrics_are_exposed() -> None:
    payload = asyncio.run(
        server.dispatch_operation(
            "rd.session.update_context",
            {"key": "notes", "value": "history-check"},
            transport="test",
        )
    )
    assert payload["ok"] is True

    history = asyncio.run(server.dispatch_operation("rd.core.get_operation_history", {"max_items": 8}, transport="test"))
    assert history["ok"] is True
    operations = history["data"]["operations"]
    assert operations
    assert operations[0]["trace_id"].startswith("trc_")
    assert operations[0]["status"] in {"completed", "running"}

    metrics = asyncio.run(server.dispatch_operation("rd.core.get_runtime_metrics", {}, transport="test"))
    assert metrics["ok"] is True
    assert metrics["data"]["metrics"]["operation_count"] >= 2
    assert "operation_duration_summary" in metrics["data"]["metrics"]
    assert metrics["data"]["recent_operations"]


def test_tool_discovery_and_graph_surface_macro_guidance() -> None:
    listed = asyncio.run(
        server.dispatch_operation(
            "rd.core.list_tools",
            {"namespace": "rd.core", "detail_level": "summary"},
            transport="test",
        )
    )
    assert listed["ok"] is True
    core_names = {tool["name"] for tool in listed["data"]["tools"]}
    assert "rd.core.get_runtime_metrics" in core_names
    assert "rd.core.search_tools" in core_names
    assert not any(name.startswith("rd.app.") for name in core_names)

    searched = asyncio.run(
        server.dispatch_operation(
            "rd.core.search_tools",
            {"query": "pixel", "detail_level": "summary"},
            transport="test",
        )
    )
    assert searched["ok"] is True
    ordered_search_names = [tool["name"] for tool in searched["data"]["tools"]]
    search_names = set(ordered_search_names)
    assert "rd.macro.explain_pixel" in search_names
    assert "rd.debug.pixel_history" in search_names
    assert ordered_search_names.index("rd.debug.pixel_history") < ordered_search_names.index("rd.macro.explain_pixel")
    assert not any(name.startswith("rd.app.") for name in search_names)

    graph = asyncio.run(
        server.dispatch_operation(
            "rd.core.get_tool_graph",
            {"query": "pixel"},
            transport="test",
        )
    )
    assert graph["ok"] is True
    assert any(edge["type"] == "macro_expands_to" and edge["from"] == "rd.macro.explain_pixel" and edge["to"] == "rd.debug.pixel_history" for edge in graph["data"]["edges"])
    assert not any(tool["name"].startswith("rd.app.") for tool in graph["data"]["tools"])
    graph_names = [tool["name"] for tool in graph["data"]["tools"]]
    assert graph_names.index("rd.debug.pixel_history") < graph_names.index("rd.macro.explain_pixel")


def test_tool_discovery_intents_follow_export_and_analysis_boundaries() -> None:
    export_list = asyncio.run(
        server.dispatch_operation(
            "rd.core.list_tools",
            {"intent": "export", "detail_level": "summary"},
            transport="test",
        )
    )
    assert export_list["ok"] is True
    export_names = {tool["name"] for tool in export_list["data"]["tools"]}
    assert {"rd.export.texture", "rd.export.buffer", "rd.export.mesh"} <= export_names
    assert "rd.texture.save_to_file" not in export_names
    assert "rd.buffer.save_to_file" not in export_names
    assert "rd.mesh.export" not in export_names

    analysis_list = asyncio.run(
        server.dispatch_operation(
            "rd.core.list_tools",
            {"intent": "analysis", "detail_level": "summary"},
            transport="test",
        )
    )
    assert analysis_list["ok"] is True
    analysis_names = {tool["name"] for tool in analysis_list["data"]["tools"]}
    assert {"rd.macro.explain_pixel", "rd.debug.pixel_history", "rd.diag.scan_common_issues", "rd.texture.compute_stats"} <= analysis_names
    assert not any(name.startswith("rd.analysis.") for name in analysis_names)


def test_tool_discovery_default_priority_and_navigation_projection_hints() -> None:
    listed = asyncio.run(
        server.dispatch_operation(
            "rd.core.list_tools",
            {"detail_level": "summary"},
            transport="test",
        )
    )
    assert listed["ok"] is True
    tools = listed["data"]["tools"]
    ordered_names = [tool["name"] for tool in tools]
    assert ordered_names.index("rd.capture.open_file") < ordered_names.index("rd.macro.explain_pixel")
    assert ordered_names.index("rd.macro.explain_pixel") < ordered_names.index("rd.session.get_context")
    assert ordered_names.index("rd.session.get_context") < ordered_names.index("rd.vfs.ls")

    vfs_list = asyncio.run(
        server.dispatch_operation(
            "rd.core.list_tools",
            {"role": "navigation", "detail_level": "summary"},
            transport="test",
        )
    )
    assert vfs_list["ok"] is True
    vfs_tool = next(tool for tool in vfs_list["data"]["tools"] if tool["name"] == "rd.vfs.ls")
    assert vfs_tool["role"] == "navigation"
    assert isinstance(vfs_tool["discovery_rank"], int)
    assert vfs_tool["supports_projection"] == {"tabular": True}
    assert vfs_tool["recommended_for"] == ["browse_only"]
    assert vfs_tool["not_primary_for"] == ["precise_debug", "export", "state_mutation", "automation"]

    browse_search = asyncio.run(
        server.dispatch_operation(
            "rd.core.search_tools",
            {"query": "browse", "detail_level": "summary"},
            transport="test",
        )
    )
    assert browse_search["ok"] is True
    assert browse_search["data"]["tools"][0]["name"].startswith("rd.vfs.")

    tsv_search = asyncio.run(
        server.dispatch_operation(
            "rd.core.search_tools",
            {"query": "tsv", "detail_level": "summary"},
            transport="test",
        )
    )
    assert tsv_search["ok"] is True
    assert tsv_search["data"]["tools"][0]["name"] == "rd.vfs.ls"
    assert tsv_search["data"]["tools"][0]["supports_projection"] == {"tabular": True}


def test_session_resume_restores_persisted_local_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    capture_path = tmp_path / "resume.rdc"
    capture_path.write_text("rdc", encoding="utf-8")
    save_context_state(
        {
            "context_id": "default",
            "current_capture_file_id": "capf_resume",
            "current_session_id": "sess_resume",
            "captures": {
                "capf_resume": {
                    "capture_file_id": "capf_resume",
                    "file_path": str(capture_path),
                    "read_only": True,
                    "driver": "Vulkan",
                    "file_size_bytes": int(capture_path.stat().st_size),
                    "file_mtime_ms": int(capture_path.stat().st_mtime * 1000),
                    "file_fingerprint": f"{int(capture_path.stat().st_size)}:{int(capture_path.stat().st_mtime * 1000)}",
                }
            },
            "sessions": {
                "sess_resume": {
                    "session_id": "sess_resume",
                    "capture_file_id": "capf_resume",
                    "rdc_path": str(capture_path),
                    "file_fingerprint": f"{int(capture_path.stat().st_size)}:{int(capture_path.stat().st_mtime * 1000)}",
                    "file_size_bytes": int(capture_path.stat().st_size),
                    "frame_index": 0,
                    "active_event_id": 202,
                    "backend_type": "local",
                    "state": "degraded",
                    "is_live": False,
                    "last_error": "daemon crashed",
                    "recovery": {"status": "degraded", "attempt_count": 1, "last_error": "daemon crashed"},
                }
            },
        },
        "default",
    )
    fake_manager = _FakeRecoverySessionManager()
    fake_controller = _FakeRecoveryController([101, 202, 303])

    async def _inline_offload(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        return fn(*args, **kwargs)

    async def _fake_get_controller(session_id: str) -> _FakeRecoveryController:
        assert session_id == "sess_resume"
        return fake_controller

    asyncio.run(server.server_runtime.runtime_startup())
    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_get_controller", _fake_get_controller)
    server.server_runtime._session_manager = fake_manager

    payload = asyncio.run(server.dispatch_operation("rd.session.get_context", {}, transport="test"))

    assert payload["ok"] is True
    assert payload["data"]["current_session_id"] == "sess_resume"
    assert payload["data"]["runtime"]["session_id"] == "sess_resume"
    assert fake_manager.created == ["sess_resume"]
    assert fake_manager.opened == [("sess_resume", str(capture_path))]
    assert fake_controller.set_calls == [202]


def test_session_select_switches_pointer_without_dropping_other_sessions(tmp_path: Path) -> None:
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    capture_a.write_text("a", encoding="utf-8")
    capture_b.write_text("b", encoding="utf-8")

    server._runtime.captures = {
        "capf_a": server.CaptureFileHandle(capture_file_id="capf_a", file_path=str(capture_a), read_only=True),
        "capf_b": server.CaptureFileHandle(capture_file_id="capf_b", file_path=str(capture_b), read_only=True),
    }
    server._runtime.replays = {
        "sess_a": server.ReplayHandle(session_id="sess_a", capture_file_id="capf_a", frame_index=0, active_event_id=11),
        "sess_b": server.ReplayHandle(session_id="sess_b", capture_file_id="capf_b", frame_index=0, active_event_id=22),
    }
    server.server_runtime._set_context_runtime_session("sess_a", capture_file_id="capf_a", backend_type="local", frame_index=0, active_event_id=11)
    server.server_runtime._set_context_runtime_session("sess_b", capture_file_id="capf_b", backend_type="local", frame_index=0, active_event_id=22)

    listed = asyncio.run(server.dispatch_operation("rd.session.list_sessions", {}, transport="test"))
    assert listed["ok"] is True
    assert len(listed["data"]["sessions"]) == 2
    assert listed["data"]["current_session_id"] == "sess_b"

    selected = asyncio.run(server.dispatch_operation("rd.session.select_session", {"session_id": "sess_a"}, transport="test"))
    assert selected["ok"] is True
    assert selected["data"]["current_session_id"] == "sess_a"
    assert len(selected["data"]["sessions"]) == 2


def test_open_replay_rejects_estimated_memory_limit(tmp_path: Path) -> None:
    capture_path = tmp_path / "large.rdc"
    capture_path.write_bytes(b"x" * 128)
    server._runtime.captures = {
        "capf_limit": server.CaptureFileHandle(capture_file_id="capf_limit", file_path=str(capture_path), read_only=True)
    }
    asyncio.run(server.server_runtime.runtime_startup())
    server.server_runtime._apply_runtime_config(
        {
            "runtime_limits": {
                "max_sessions_per_context": 4,
                "max_estimated_replay_memory_bytes": 64,
                "replay_memory_multiplier": 2.0,
            }
        }
    )

    payload = asyncio.run(server.dispatch_operation("rd.capture.open_replay", {"capture_file_id": "capf_limit", "options": {}}, transport="test"))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "replay_memory_limit_exceeded"

