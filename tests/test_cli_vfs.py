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


def test_vfs_command_routes_to_direct_exec(monkeypatch) -> None:
    captured: list[dict] = []

    async def _fake_direct_exec(operation: str, args: dict[str, object], *, remote: bool = False):  # type: ignore[no-untyped-def]
        assert operation == "rd.vfs.resolve"
        assert args == {"path": "/pipeline", "session_id": "sess_demo"}
        return {"ok": True, "data": {"resolved": {"path": "/pipeline"}}}

    monkeypatch.setattr(rdx_cli, "_direct_exec", _fake_direct_exec)
    monkeypatch.setattr(rdx_cli, "_print_json", lambda payload: captured.append(payload))

    args = argparse.Namespace(
        command="vfs",
        vfs_cmd="resolve",
        path="/pipeline",
        session_id="sess_demo",
        json=True,
        connect=False,
        daemon_context="default",
    )
    exit_code = asyncio.run(rdx_cli._main_async(args))

    assert exit_code == rdx_cli.EXIT_OK
    assert captured[0]["ok"] is True
    assert captured[0]["data"]["resolved"]["path"] == "/pipeline"


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
        json=True,
        connect=True,
        daemon_context="ctx-vfs",
    )
    exit_code = asyncio.run(rdx_cli._main_async(args))

    assert exit_code == rdx_cli.EXIT_OK
    assert captured[0]["ok"] is True
    assert captured[0]["data"]["tree"]["path"] == "/draws"
