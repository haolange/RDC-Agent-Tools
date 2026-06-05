from __future__ import annotations

import argparse
import asyncio

from rdx import cli as rdx_cli


def test_build_parser_accepts_vfs_tree_command() -> None:
    parser = rdx_cli._build_parser()
    args = parser.parse_args(["vfs", "tree", "--path", "/draws", "--depth", "3"])

    assert args.command == "vfs"
    assert args.vfs_cmd == "tree"
    assert args.path == "/draws"
    assert args.depth == 3


def test_build_parser_accepts_cli_first_doctor_and_tools() -> None:
    parser = rdx_cli._build_parser()

    doctor = parser.parse_args(["--json", "doctor"])
    version = parser.parse_args(["version", "--json"])
    completion = parser.parse_args(["completion", "powershell"])
    context_status = parser.parse_args(["context", "status", "--json"])
    context_update = parser.parse_args(["context", "update", "--key", "notes", "--value", "triaged", "--json"])
    context_list = parser.parse_args(["context", "list", "--json"])
    context_clear = parser.parse_args(["context", "clear", "--json"])
    tools_list = parser.parse_args(["tools", "list", "--json", "--limit", "3"])
    tools_search = parser.parse_args(["tools", "search", "pipeline", "--json"])

    assert doctor.command == "doctor"
    assert doctor.json is True
    assert version.command == "version"
    assert version.json is True
    assert completion.command == "completion"
    assert completion.shell == "powershell"
    assert context_status.command == "context"
    assert context_status.context_cmd == "status"
    assert context_update.context_cmd == "update"
    assert context_update.key == "notes"
    assert context_list.context_cmd == "list"
    assert context_clear.context_cmd == "clear"
    assert tools_list.command == "tools"
    assert tools_list.tools_cmd == "list"
    assert tools_list.limit == 3
    assert tools_search.command == "tools"
    assert tools_search.tools_cmd == "search"
    assert tools_search.query == "pipeline"


def test_doctor_reports_cli_only_contract(monkeypatch) -> None:
    captured: list[dict] = []

    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))
    monkeypatch.setattr(rdx_cli, "_daemon_status_payload", lambda context: {"ok": True, "data": {"running": False, "context_id": context}})
    monkeypatch.setattr(rdx_cli, "missing_dependencies", lambda: [])
    monkeypatch.setattr(
        rdx_cli,
        "validate_bundled_python_layout",
        lambda: (True, [], {"bundled_python": {"python_version": "test", "python_entry": "python.exe"}}),
    )

    args = argparse.Namespace(command="doctor", daemon_context="ctx-doctor", json=True)
    exit_code = asyncio.run(rdx_cli._main_async(args))

    assert exit_code == rdx_cli.EXIT_OK
    assert captured[0]["ok"] is True
    details = captured[0]["data"]
    assert details["context_id"] == "ctx-doctor"
    assert details["mcp"]["supported"] is False
    assert details["launchers"]["python_cli_exists"] is True


def test_tools_list_and_search_emit_catalog_summaries(monkeypatch) -> None:
    captured: list[dict] = []
    fake_catalog = [
        {
            "name": "rd.pipeline.get_state",
            "namespace": "pipeline",
            "group": "Pipeline",
            "description": "Get pipeline state",
            "param_names": ["session_id"],
        },
        {
            "name": "rd.capture.status",
            "namespace": "capture",
            "group": "Capture",
            "description": "Get capture status",
            "param_names": [],
        },
    ]

    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))
    monkeypatch.setattr(rdx_cli, "load_tool_catalog", lambda: fake_catalog)

    list_code = asyncio.run(
        rdx_cli._main_async(argparse.Namespace(command="tools", tools_cmd="list", namespace="", limit=0, daemon_context="default")),
    )
    search_code = asyncio.run(
        rdx_cli._main_async(argparse.Namespace(command="tools", tools_cmd="search", query="pipeline", limit=20, daemon_context="default")),
    )

    assert list_code == rdx_cli.EXIT_OK
    assert search_code == rdx_cli.EXIT_OK
    assert captured[0]["result_kind"] == "rdx.tools.list"
    assert captured[0]["data"]["tool_count"] == 2
    assert captured[1]["result_kind"] == "rdx.tools.search"
    assert captured[1]["data"]["tool_count"] == 1
    assert captured[1]["data"]["tools"][0]["name"] == "rd.pipeline.get_state"


def test_version_command_emits_stable_json(monkeypatch) -> None:
    captured: list[dict] = []

    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    exit_code = asyncio.run(
        rdx_cli._main_async(argparse.Namespace(command="version", json=True, daemon_context="default")),
    )

    assert exit_code == rdx_cli.EXIT_OK
    assert captured[0]["ok"] is True
    assert captured[0]["result_kind"] == "rdx.version"
    assert captured[0]["data"]["compatibility"]["json_envelope"] == "stable"
    assert captured[0]["data"]["compatibility"]["mcp_supported"] is False


def test_completion_command_outputs_shell_script(monkeypatch, capsys) -> None:
    monkeypatch.setattr(rdx_cli, "load_tool_catalog", lambda: [{"name": "rd.session.get_context"}])

    exit_code = asyncio.run(
        rdx_cli._main_async(argparse.Namespace(command="completion", shell="powershell", daemon_context="default")),
    )

    assert exit_code == rdx_cli.EXIT_OK
    output = capsys.readouterr().out
    assert "Register-ArgumentCompleter" in output
    assert "rd.session.get_context" in output


def test_context_commands_route_to_canonical_session_tools(monkeypatch) -> None:
    captured: list[dict] = []
    seen: list[tuple[str, dict[str, object], str]] = []

    def _fake_daemon_exec(operation: str, args: dict[str, object], *, remote: bool = False, context: str = "default"):  # type: ignore[no-untyped-def]
        seen.append((operation, dict(args), context))
        return {"ok": True, "result_kind": operation, "data": {"context_id": context}, "artifacts": [], "error": None, "meta": {}, "projections": {}}

    monkeypatch.setattr(rdx_cli, "_daemon_exec", _fake_daemon_exec)
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    status_code = asyncio.run(
        rdx_cli._main_async(argparse.Namespace(command="context", context_cmd="status", daemon_context="ctx-agent", json=True)),
    )
    update_code = asyncio.run(
        rdx_cli._main_async(
            argparse.Namespace(
                command="context",
                context_cmd="update",
                key="notes",
                value='{"summary":"triaged"}',
                daemon_context="ctx-agent",
                json=True,
            ),
        ),
    )
    list_code = asyncio.run(
        rdx_cli._main_async(argparse.Namespace(command="context", context_cmd="list", daemon_context="ctx-agent", json=True)),
    )

    assert status_code == rdx_cli.EXIT_OK
    assert update_code == rdx_cli.EXIT_OK
    assert list_code == rdx_cli.EXIT_OK
    assert seen == [
        ("rd.session.get_context", {}, "ctx-agent"),
        ("rd.session.update_context", {"key": "notes", "value": {"summary": "triaged"}}, "ctx-agent"),
        ("rd.session.list_contexts", {}, "ctx-agent"),
    ]
    assert captured[0]["result_kind"] == "rd.session.get_context"


def test_session_preview_status_without_daemon_is_successful_status(monkeypatch) -> None:
    captured: list[dict] = []

    monkeypatch.setattr(
        rdx_cli,
        "_daemon_status_payload",
        lambda context: {"ok": True, "data": {"running": False, "state": {"context_id": context}}},
    )
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    exit_code = asyncio.run(
        rdx_cli._main_async(
            argparse.Namespace(
                command="session",
                session_cmd="preview",
                session_preview_cmd="status",
                daemon_context="ctx-preview",
            ),
        ),
    )

    assert exit_code == rdx_cli.EXIT_OK
    assert captured[0]["result_kind"] == "rdx.session.preview.status"
    assert captured[0]["data"]["running"] is False
    assert captured[0]["data"]["has_session"] is False


def test_vfs_command_routes_to_direct_exec(monkeypatch) -> None:
    captured: list[dict] = []

    def _fake_daemon_exec(operation: str, args: dict[str, object], *, remote: bool = False, context: str = "default"):  # type: ignore[no-untyped-def]
        assert operation == "rd.vfs.resolve"
        assert args == {"path": "/pipeline", "session_id": "sess_demo"}
        assert context == "default"
        return {"ok": True, "data": {"node": {"path": "/pipeline"}}, "projections": {}, "meta": {}}

    monkeypatch.setattr(rdx_cli, "_daemon_exec", _fake_daemon_exec)
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    args = argparse.Namespace(
        command="vfs",
        vfs_cmd="resolve",
        path="/pipeline",
        session_id="sess_demo",
        format="json",
        daemon_context="default",
    )
    exit_code = asyncio.run(rdx_cli._main_async(args))

    assert exit_code == rdx_cli.EXIT_OK
    assert captured[0]["ok"] is True
    assert captured[0]["data"]["node"]["path"] == "/pipeline"


def test_vfs_command_routes_to_daemon_exec(monkeypatch) -> None:
    captured: list[dict] = []

    def _fake_daemon_exec(operation: str, args: dict[str, object], *, remote: bool = False, context: str = "default"):  # type: ignore[no-untyped-def]
        assert operation == "rd.vfs.tree"
        assert args == {"path": "/draws", "depth": 2}
        assert context == "ctx-vfs"
        return {"ok": True, "data": {"tree": {"path": "/draws"}}}

    monkeypatch.setattr(rdx_cli, "_daemon_exec", _fake_daemon_exec)
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    args = argparse.Namespace(
        command="vfs",
        vfs_cmd="tree",
        path="/draws",
        session_id=None,
        depth=2,
        format="json",
        daemon_context="ctx-vfs",
    )
    exit_code = asyncio.run(rdx_cli._main_async(args))

    assert exit_code == rdx_cli.EXIT_OK
    assert captured[0]["ok"] is True
    assert captured[0]["data"]["tree"]["path"] == "/draws"


def test_vfs_ls_tsv_renders_daemon_projection(monkeypatch, capsys) -> None:
    def _fake_daemon_exec(operation: str, args: dict[str, object], *, remote: bool = False, context: str = "default"):  # type: ignore[no-untyped-def]
        assert operation == "rd.vfs.ls"
        assert args["projection"] == {"kind": "tabular", "include_tsv_text": True}
        return {
            "ok": True,
            "data": {"path": "/", "entries": []},
            "artifacts": [],
            "error": None,
            "meta": {},
            "projections": {
                "tabular": {
                    "format_version": "1.0.0",
                    "columns": ["format_version", "name", "path"],
                    "rows": [["1.0.0", "context", "/context"]],
                    "row_count": 1,
                    "tsv_text": "format_version\tname\tpath\n1.0.0\tcontext\t/context",
                }
            },
        }

    monkeypatch.setattr(rdx_cli, "_daemon_exec", _fake_daemon_exec)

    args = argparse.Namespace(
        command="vfs",
        vfs_cmd="ls",
        path="/",
        session_id=None,
        format="tsv",
        daemon_context="ctx-vfs",
    )

    exit_code = asyncio.run(rdx_cli._main_async(args))

    assert exit_code == rdx_cli.EXIT_OK
    assert "format_version\tname\tpath" in capsys.readouterr().out


def test_tsv_missing_projection_returns_stable_validation_error(monkeypatch) -> None:
    captured: list[dict] = []

    def _fake_daemon_exec(operation: str, args: dict[str, object], *, remote: bool = False, context: str = "default"):  # type: ignore[no-untyped-def]
        assert args["projection"] == {"kind": "tabular", "include_tsv_text": True}
        return {"ok": True, "result_kind": operation, "data": {"context_id": context}, "artifacts": [], "error": None, "meta": {}, "projections": {}}

    monkeypatch.setattr(rdx_cli, "_daemon_exec", _fake_daemon_exec)
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    exit_code = asyncio.run(
        rdx_cli._main_async(
            argparse.Namespace(
                command="call",
                operation="rd.session.get_context",
                args_json=None,
                args_file=None,
                format="tsv",
                remote=False,
                daemon_context="ctx-agent",
            ),
        ),
    )

    assert exit_code == rdx_cli.EXIT_RUNTIME_ERR
    assert captured[0]["ok"] is False
    assert captured[0]["error"]["code"] == "tabular_projection_missing"
    assert captured[0]["error"]["details"]["requested_format"] == "tsv"


def test_pipeline_diff_and_assert_without_session_return_session_required(monkeypatch) -> None:
    captured: list[dict] = []

    monkeypatch.setattr(rdx_cli, "_default_session_id", lambda value, context="default": (_ for _ in ()).throw(RuntimeError("No session_id available.")))
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    diff_code = asyncio.run(
        rdx_cli._main_async(
            argparse.Namespace(
                command="diff",
                diff_cmd="pipeline",
                session_id=None,
                event_a=1,
                event_b=2,
                fail_on_diff=False,
                daemon_context="ctx-agent",
            ),
        ),
    )
    assert_code = asyncio.run(
        rdx_cli._main_async(
            argparse.Namespace(
                command="assert",
                assert_cmd="pipeline",
                session_id=None,
                event_a=1,
                event_b=2,
                max_changes=0,
                daemon_context="ctx-agent",
            ),
        ),
    )

    assert diff_code == rdx_cli.EXIT_RUNTIME_ERR
    assert assert_code == rdx_cli.EXIT_RUNTIME_ERR
    assert captured[0]["error"]["code"] == "session_required"
    assert captured[0]["error"]["details"]["context_id"] == "ctx-agent"
    assert captured[1]["error"]["code"] == "session_required"
