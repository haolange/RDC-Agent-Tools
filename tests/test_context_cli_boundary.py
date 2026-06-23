from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rdx import server
from rdx.context_snapshot import clear_context_snapshot
from rdx.runtime_state import clear_context_state


PRE_GA_CONTEXT_FIELDS = {
    "runtime_" + "owner",
    "owner_" + "lease",
    "active_" + "baton",
    "rehydrate_" + "status",
    "runtime_" + "parallelism_" + "ceiling",
    "entry_" + "mode",
}
PRE_GA_SESSION_TOOLS = {
    "rd.session.claim_" + "runtime_" + "owner",
    "rd.session.release_" + "runtime_" + "owner",
    "rd.session.export_" + "runtime_" + "baton",
    "rd.session.rehydrate_" + "runtime_" + "baton",
}


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
    for context_id in ("default", "ctx-alpha"):
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
        for context_id in ("default", "ctx-alpha"):
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


def test_context_lifecycle_tools_do_not_expose_pre_ga_coordination_fields() -> None:
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
    assert default_snapshot["data"]["session_locator"] == {"rdc_path": "", "session_id": "", "frame_index": 0, "active_event_id": 0}
    assert PRE_GA_CONTEXT_FIELDS.isdisjoint(default_snapshot["data"])

    listed = asyncio.run(server.dispatch_operation("rd.session.list_contexts", {}, transport="test"))
    assert listed["ok"] is True
    contexts = listed["data"]["contexts"]
    context_ids = {item["context_id"] for item in contexts}
    assert {"default", "ctx-alpha"} <= context_ids
    assert all("session_locator" in item for item in contexts)
    assert all(PRE_GA_CONTEXT_FIELDS.isdisjoint(item) for item in contexts)

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
    assert PRE_GA_CONTEXT_FIELDS.isdisjoint(selected["data"])

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
    assert PRE_GA_CONTEXT_FIELDS.isdisjoint(cleared["data"])


def test_docs_and_catalog_keep_pre_ga_coordination_markers_out_of_public_contract() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    readme = (repo_root / "README.md").read_text(encoding="utf-8-sig")
    session_doc = (repo_root / "docs" / "session-model.md").read_text(encoding="utf-8-sig")
    agent_doc = (repo_root / "docs" / "agent-model.md").read_text(encoding="utf-8-sig")
    catalog = json.loads((repo_root / "spec" / "tool_catalog.json").read_text(encoding="utf-8-sig"))
    tools = {item["name"]: item for item in catalog.get("tools") or []}

    forbidden_doc_terms = {
        "staged_" + "handoff",
        "orchestrated multi-" + "context",
        "stricter ownership",
        "single_" + "runtime_" + "owner",
        "multi_" + "context_multi_owner",
    }
    for text in (readme, session_doc, agent_doc):
        assert "session_locator" in text
        assert all(term not in text for term in forbidden_doc_terms)

    assert PRE_GA_SESSION_TOOLS.isdisjoint(tools)
    assert "session_locator" in tools["rd.session.get_context"]["returns_raw"]
    assert "session_locator" in tools["rd.session.list_contexts"]["returns_raw"]
    for tool in tools.values():
        param_names = {str(name) for name in tool.get("param_names", [])}
        assert {"runtime_" + "owner", "owner_" + "lease_id", "baton_" + "id"}.isdisjoint(param_names)
        returns_raw = str(tool.get("returns_raw") or "")
        assert all(field not in returns_raw for field in PRE_GA_CONTEXT_FIELDS)
