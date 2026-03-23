"""RDX CLI adapter backed by the daemon-owned runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from rdx.core.assert_service import AssertService
from rdx.core.contracts import canonical_error, canonical_success
from rdx.daemon.client import (
    attach_client,
    cleanup_stale_daemon_states,
    clear_context,
    daemon_request,
    detach_client,
    ensure_daemon,
    heartbeat_client,
    load_daemon_state,
    stop_daemon,
)
from rdx.runtime_paths import artifacts_dir
from rdx.timeout_policy import daemon_exec_timeout_s

EXIT_OK = 0
EXIT_ASSERT_FAIL = 1
EXIT_RUNTIME_ERR = 2


def _print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_args_json(raw: str) -> Dict[str, Any]:
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--args-json must be a JSON object")
    return parsed


def _extract(payload: Dict[str, Any], key: str, default: Any = None) -> Any:
    data = payload.get("data")
    if isinstance(data, dict) and key in data:
        return data.get(key, default)
    return payload.get(key, default)


def _ensure_daemon_state(context: str) -> Dict[str, Any]:
    ok, message, state = ensure_daemon(context=context)
    if not ok:
        raise RuntimeError(message)
    if not state:
        raise RuntimeError("daemon did not return state")
    return state


def _daemon_exec(
    operation: str,
    args: Dict[str, Any],
    *,
    remote: bool = False,
    context: str = "default",
) -> Dict[str, Any]:
    state = _ensure_daemon_state(context)
    resp = daemon_request(
        "exec",
        params={"operation": operation, "args": args, "transport": "cli", "remote": remote},
        timeout=daemon_exec_timeout_s(operation, args),
        context=context,
        state=state,
    )
    if not bool(resp.get("ok")):
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        raise RuntimeError(str(err.get("message") or "daemon exec failed"))
    result = resp.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned invalid result payload")
    return result


def _daemon_status_payload(context: str) -> Dict[str, Any]:
    state = load_daemon_state(context=context)
    if not state:
        cleanup_stale_daemon_states(context=context)
        state = load_daemon_state(context=context)
    if not state:
        return canonical_success(
            result_kind="rdx.daemon.status",
            data={"running": False, "state": {"context_id": context, "daemon_context": context}},
            transport="cli",
        )
    try:
        resp = daemon_request("status", params={}, context=context, state=state)
    except Exception:
        cleaned = cleanup_stale_daemon_states(context=context)
        refreshed = load_daemon_state(context=context)
        if not refreshed:
            return canonical_success(
                result_kind="rdx.daemon.status",
                data={"running": False, "state": {"context_id": context, "daemon_context": context}, "cleaned": cleaned},
                transport="cli",
            )
        return canonical_error(
            result_kind="rdx.daemon.status",
            code="runtime_error",
            category="runtime",
            message="daemon status failed",
            details={"state": refreshed, "cleaned": cleaned},
            transport="cli",
        )
    result = resp.get("result", {}) if isinstance(resp, dict) else {}
    running = bool(result.get("running", True)) if isinstance(result, dict) else True
    daemon_state = result.get("state") if isinstance(result, dict) else {}
    return canonical_success(
        result_kind="rdx.daemon.status",
        data={"running": running, "state": daemon_state if isinstance(daemon_state, dict) else state},
        transport="cli",
    )


def _default_session_id(cli_value: Optional[str], context: str = "default") -> str:
    if cli_value:
        return str(cli_value)
    state = _ensure_daemon_state(context)
    resp = daemon_request("get_state", params={}, context=context, state=state)
    if bool(resp.get("ok")):
        daemon_state = resp.get("result", {}).get("state", {})
        if isinstance(daemon_state, dict):
            session_id = str(daemon_state.get("session_id") or "").strip()
            if session_id:
                return session_id
    snapshot = _daemon_exec("rd.session.get_context", {}, context=context)
    runtime_payload = snapshot.get("data", {}).get("runtime", {}) if isinstance(snapshot.get("data"), dict) else {}
    session_id = str(runtime_payload.get("session_id") or "").strip() if isinstance(runtime_payload, dict) else ""
    if session_id:
        return session_id
    raise RuntimeError("No session_id available. Use `rdx capture open --file <rdc>` first or pass --session-id.")


def _tabular_request(output_format: str, call_args: Dict[str, Any]) -> Dict[str, Any]:
    if output_format != "tsv":
        return dict(call_args)
    projection = call_args.get("projection")
    if projection is None:
        normalized_projection: Dict[str, Any] = {}
    elif isinstance(projection, dict):
        normalized_projection = dict(projection)
    else:
        raise ValueError("projection must be a JSON object")
    normalized_projection["kind"] = "tabular"
    normalized_projection["include_tsv_text"] = True
    patched = dict(call_args)
    patched["projection"] = normalized_projection
    return patched


def _render_tabular(payload: Dict[str, Any]) -> None:
    projections = payload.get("projections")
    if not isinstance(projections, dict):
        raise RuntimeError("tool did not return tabular projection")
    tabular = projections.get("tabular")
    if not isinstance(tabular, dict):
        raise RuntimeError("tool did not return tabular projection")
    text = str(tabular.get("tsv_text") or "").strip()
    if text:
        print(text)
        return
    columns = tabular.get("columns")
    rows = tabular.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise RuntimeError("tabular projection is missing columns/rows")
    print("\t".join(str(col) for col in columns))
    for row in rows:
        if not isinstance(row, list):
            raise RuntimeError("tabular row must be a list")
        print("\t".join("" if item is None else str(item) for item in row))


def _render_result(payload: Dict[str, Any], *, output_format: str = "json") -> None:
    if output_format == "tsv" and bool(payload.get("ok")):
        _render_tabular(payload)
        return
    _print_json(payload)


async def _cmd_call(args: argparse.Namespace) -> int:
    call_args = _tabular_request(str(args.format), _parse_args_json(args.args_json or "{}"))
    payload = _daemon_exec(args.operation, call_args, remote=bool(args.remote), context=str(args.daemon_context))
    _render_result(payload, output_format=str(args.format))
    return EXIT_OK if bool(payload.get("ok")) else EXIT_RUNTIME_ERR


async def _cmd_vfs(args: argparse.Namespace) -> int:
    op = f"rd.vfs.{args.vfs_cmd}"
    call_args: Dict[str, Any] = {"path": str(args.path or "/")}
    if getattr(args, "session_id", None):
        call_args["session_id"] = str(args.session_id)
    if args.vfs_cmd == "tree":
        call_args["depth"] = int(args.depth)
    payload = _daemon_exec(op, _tabular_request(str(args.format), call_args), context=str(args.daemon_context))
    _render_result(payload, output_format=str(args.format))
    return EXIT_OK if bool(payload.get("ok")) else EXIT_RUNTIME_ERR


async def _cmd_capture_open(args: argparse.Namespace) -> int:
    file_path = str(Path(args.file).resolve())
    context = str(args.daemon_context)

    init_payload = _daemon_exec(
        "rd.core.init",
        {
            "global_env": {"artifact_dir": str(Path(args.artifact_dir).resolve())},
            "enable_remote": True,
        },
        context=context,
    )
    if not bool(init_payload.get("ok")):
        _print_json(init_payload)
        return EXIT_RUNTIME_ERR

    open_file = _daemon_exec("rd.capture.open_file", {"file_path": file_path, "read_only": True}, context=context)
    if not bool(open_file.get("ok")):
        _print_json(open_file)
        return EXIT_RUNTIME_ERR
    capture_file_id = str(_extract(open_file, "capture_file_id") or "")

    open_replay = _daemon_exec(
        "rd.capture.open_replay",
        {"capture_file_id": capture_file_id, "options": {}},
        context=context,
    )
    if not bool(open_replay.get("ok")):
        _print_json(open_replay)
        return EXIT_RUNTIME_ERR
    session_id = str(_extract(open_replay, "session_id") or "")

    set_frame = _daemon_exec(
        "rd.replay.set_frame",
        {"session_id": session_id, "frame_index": int(args.frame_index)},
        context=context,
    )
    if not bool(set_frame.get("ok")):
        _print_json(set_frame)
        return EXIT_RUNTIME_ERR

    context_payload = _daemon_exec("rd.session.get_context", {}, context=context)
    runtime_snapshot = context_payload.get("data", {}).get("runtime", {}) if isinstance(context_payload.get("data"), dict) else {}
    payload = canonical_success(
        result_kind="rdx.capture.open",
        data={
            "context_id": context,
            "capture_file_id": capture_file_id,
            "session_id": session_id,
            "active_event_id": int(_extract(set_frame, "active_event_id", 0) or 0),
            "runtime": runtime_snapshot if isinstance(runtime_snapshot, dict) else {},
            "context": context_payload.get("data") if isinstance(context_payload.get("data"), dict) else {},
        },
        transport="cli",
    )
    _print_json(payload)
    return EXIT_OK


def _cmd_capture_status(args: argparse.Namespace) -> int:
    context = str(args.daemon_context)
    state = _ensure_daemon_state(context)
    daemon_state_resp = daemon_request("get_state", params={}, context=context, state=state)
    daemon_state = daemon_state_resp.get("result", {}).get("state", {}) if isinstance(daemon_state_resp, dict) else {}
    snapshot_payload = _daemon_exec("rd.session.get_context", {}, context=context)
    snapshot = snapshot_payload.get("data") if isinstance(snapshot_payload.get("data"), dict) else {}
    runtime_payload = snapshot.get("runtime", {}) if isinstance(snapshot, dict) else {}
    has_session = bool(str(runtime_payload.get("session_id") or "").strip()) if isinstance(runtime_payload, dict) else False
    payload = canonical_success(
        result_kind="rdx.capture.status",
        data={
            "context_id": context,
            "has_session": has_session,
            "state": daemon_state if isinstance(daemon_state, dict) else {},
            "context": snapshot if isinstance(snapshot, dict) else {},
        },
        transport="cli",
    )
    _print_json(payload)
    return EXIT_OK


async def _cmd_diff_pipeline(args: argparse.Namespace) -> int:
    session_id = _default_session_id(args.session_id, context=str(args.daemon_context))
    payload = _daemon_exec(
        "rd.event.diff_pipeline_state",
        {"session_id": session_id, "event_a": int(args.event_a), "event_b": int(args.event_b)},
        context=str(args.daemon_context),
    )
    _print_json(payload)
    if not bool(payload.get("ok")):
        return EXIT_RUNTIME_ERR
    changes = _extract(payload, "diff", [])
    has_diff = isinstance(changes, list) and len(changes) > 0
    if args.fail_on_diff and has_diff:
        return EXIT_ASSERT_FAIL
    return EXIT_OK


async def _cmd_diff_image(args: argparse.Namespace) -> int:
    diff_args = {
        "image_a_path": str(Path(args.image_a).resolve()),
        "image_b_path": str(Path(args.image_b).resolve()),
        "output_path": str(Path(args.out).resolve()) if args.out else None,
    }
    payload = _daemon_exec("rd.util.diff_images", diff_args, context=str(args.daemon_context))
    _print_json(payload)
    if not bool(payload.get("ok")):
        return EXIT_RUNTIME_ERR
    if args.threshold is None:
        return EXIT_OK
    metrics = _extract(payload, "metrics", {})
    mse = float(metrics.get("mse", 0.0)) if isinstance(metrics, dict) else 0.0
    return EXIT_OK if mse <= float(args.threshold) else EXIT_ASSERT_FAIL


async def _cmd_assert_pipeline(args: argparse.Namespace) -> int:
    session_id = _default_session_id(args.session_id, context=str(args.daemon_context))
    payload = _daemon_exec(
        "rd.event.diff_pipeline_state",
        {"session_id": session_id, "event_a": int(args.event_a), "event_b": int(args.event_b)},
        context=str(args.daemon_context),
    )
    if not bool(payload.get("ok")):
        _print_json(
            canonical_error(
                result_kind="rdx.assert.pipeline",
                code="runtime_error",
                category="runtime",
                message=str((payload.get("error") or {}).get("message") or "pipeline diff failed"),
                details={"source": payload},
                transport="cli",
            ),
        )
        return EXIT_RUNTIME_ERR
    outcome = AssertService.assert_pipeline_diff(payload, max_changes=int(args.max_changes))
    result = canonical_success(
        result_kind="rdx.assert.pipeline",
        data={"pass": outcome.passed, "reason": outcome.reason, "details": outcome.details},
        transport="cli",
    )
    _print_json(result)
    return EXIT_OK if outcome.passed else EXIT_ASSERT_FAIL


async def _cmd_assert_image(args: argparse.Namespace) -> int:
    diff_args = {
        "image_a_path": str(Path(args.image_a).resolve()),
        "image_b_path": str(Path(args.image_b).resolve()),
        "output_path": str(Path(args.out).resolve()) if args.out else None,
    }
    payload = _daemon_exec("rd.util.diff_images", diff_args, context=str(args.daemon_context))
    if not bool(payload.get("ok")):
        _print_json(
            canonical_error(
                result_kind="rdx.assert.image",
                code="runtime_error",
                category="runtime",
                message=str((payload.get("error") or {}).get("message") or "image diff failed"),
                details={"source": payload},
                transport="cli",
            ),
        )
        return EXIT_RUNTIME_ERR
    outcome = AssertService.assert_image_metrics(
        payload,
        mse_max=float(args.mse_max) if args.mse_max is not None else None,
        max_abs_max=float(args.max_abs_max) if args.max_abs_max is not None else None,
        psnr_min=float(args.psnr_min) if args.psnr_min is not None else None,
    )
    result = canonical_success(
        result_kind="rdx.assert.image",
        data={"pass": outcome.passed, "reason": outcome.reason, "details": outcome.details},
        transport="cli",
    )
    _print_json(result)
    return EXIT_OK if outcome.passed else EXIT_ASSERT_FAIL


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rdx", description="RDX daemon-backed CLI")
    parser.add_argument("--daemon-context", default="default", help="Daemon state namespace (default: default)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_daemon = sub.add_parser("daemon", help="Daemon lifecycle")
    s_daemon = p_daemon.add_subparsers(dest="daemon_cmd", required=True)
    p_daemon_start = s_daemon.add_parser("start")
    p_daemon_start.add_argument("--pipe-name", default=None)
    p_daemon_start.add_argument("--owner-pid", type=int, default=None, help="Optional launcher shell PID used for auto stop")
    s_daemon.add_parser("stop")
    s_daemon.add_parser("status")
    p_daemon_attach = s_daemon.add_parser("attach", help=argparse.SUPPRESS)
    p_daemon_attach.add_argument("--client-id", required=True)
    p_daemon_attach.add_argument("--client-type", default="cli")
    p_daemon_attach.add_argument("--pid", type=int, default=0)
    p_daemon_attach.add_argument("--lease-timeout-seconds", type=int, default=120)
    p_daemon_heartbeat = s_daemon.add_parser("heartbeat", help=argparse.SUPPRESS)
    p_daemon_heartbeat.add_argument("--client-id", required=True)
    p_daemon_heartbeat.add_argument("--pid", type=int, default=0)
    p_daemon_detach = s_daemon.add_parser("detach", help=argparse.SUPPRESS)
    p_daemon_detach.add_argument("--client-id", required=True)
    s_daemon.add_parser("cleanup", help=argparse.SUPPRESS)

    p_context = sub.add_parser("context", help="Context state helpers")
    s_context = p_context.add_subparsers(dest="context_cmd", required=True)
    s_context.add_parser("clear")

    p_call = sub.add_parser("call", help="Call any rd.* operation")
    p_call.add_argument("operation")
    p_call.add_argument("--args-json", default="{}")
    p_call.add_argument("--format", choices=("json", "tsv"), default="json")
    p_call.add_argument("--remote", action="store_true")

    p_capture = sub.add_parser("capture", help="Capture session helpers")
    s_capture = p_capture.add_subparsers(dest="capture_cmd", required=True)
    p_capture_open = s_capture.add_parser("open")
    p_capture_open.add_argument("--file", required=True)
    p_capture_open.add_argument("--frame-index", type=int, default=0)
    p_capture_open.add_argument("--artifact-dir", default=str(artifacts_dir().resolve()))
    s_capture.add_parser("status")

    p_vfs = sub.add_parser("vfs", help="Read-only VFS navigation helpers")
    s_vfs = p_vfs.add_subparsers(dest="vfs_cmd", required=True)
    for name in ("ls", "cat", "resolve"):
        p_vfs_cmd = s_vfs.add_parser(name)
        p_vfs_cmd.add_argument("--path", default="/")
        p_vfs_cmd.add_argument("--session-id", default=None)
        p_vfs_cmd.add_argument("--format", choices=("json", "tsv"), default="json")
    p_vfs_tree = s_vfs.add_parser("tree")
    p_vfs_tree.add_argument("--path", default="/")
    p_vfs_tree.add_argument("--session-id", default=None)
    p_vfs_tree.add_argument("--depth", type=int, default=2)
    p_vfs_tree.add_argument("--format", choices=("json", "tsv"), default="json")

    p_diff = sub.add_parser("diff", help="Diff commands")
    s_diff = p_diff.add_subparsers(dest="diff_cmd", required=True)
    p_diff_pipeline = s_diff.add_parser("pipeline")
    p_diff_pipeline.add_argument("--session-id", default=None)
    p_diff_pipeline.add_argument("--event-a", required=True, type=int)
    p_diff_pipeline.add_argument("--event-b", required=True, type=int)
    p_diff_pipeline.add_argument("--fail-on-diff", action="store_true", help="Return exit code 1 if any diff exists")
    p_diff_image = s_diff.add_parser("image")
    p_diff_image.add_argument("--image-a", required=True)
    p_diff_image.add_argument("--image-b", required=True)
    p_diff_image.add_argument("--out", default=None)
    p_diff_image.add_argument("--threshold", type=float, default=None)

    p_assert = sub.add_parser("assert", help="Assertion commands")
    s_assert = p_assert.add_subparsers(dest="assert_cmd", required=True)
    p_assert_pipeline = s_assert.add_parser("pipeline")
    p_assert_pipeline.add_argument("--session-id", default=None)
    p_assert_pipeline.add_argument("--event-a", required=True, type=int)
    p_assert_pipeline.add_argument("--event-b", required=True, type=int)
    p_assert_pipeline.add_argument("--max-changes", type=int, default=0)
    p_assert_image = s_assert.add_parser("image")
    p_assert_image.add_argument("--image-a", required=True)
    p_assert_image.add_argument("--image-b", required=True)
    p_assert_image.add_argument("--out", default=None)
    p_assert_image.add_argument("--mse-max", type=float, default=None)
    p_assert_image.add_argument("--max-abs-max", type=float, default=None)
    p_assert_image.add_argument("--psnr-min", type=float, default=None)

    return parser


async def _main_async(args: argparse.Namespace) -> int:
    ctx = str(args.daemon_context)
    if args.command == "daemon":
        if args.daemon_cmd == "start":
            cleanup_stale_daemon_states(context=ctx)
            ok, message, state = ensure_daemon(
                pipe_name=args.pipe_name,
                context=ctx,
                owner_pid=args.owner_pid if hasattr(args, "owner_pid") else None,
            )
            payload = canonical_success(result_kind="rdx.daemon.start", data={"message": message, "state": state}, transport="cli") if ok else canonical_error(result_kind="rdx.daemon.start", code="runtime_error", category="runtime", message=message, transport="cli")
            _print_json(payload)
            return EXIT_OK if ok else EXIT_RUNTIME_ERR
        if args.daemon_cmd == "stop":
            ok, message = stop_daemon(context=ctx)
            payload = canonical_success(result_kind="rdx.daemon.stop", data={"message": message}, transport="cli") if ok else canonical_error(result_kind="rdx.daemon.stop", code="runtime_error", category="runtime", message=message, transport="cli")
            _print_json(payload)
            return EXIT_OK if ok else EXIT_RUNTIME_ERR
        if args.daemon_cmd == "status":
            payload = _daemon_status_payload(ctx)
            _print_json(payload)
            return EXIT_OK if bool(payload.get("ok")) else EXIT_RUNTIME_ERR
        if args.daemon_cmd == "attach":
            ok, message, state = attach_client(
                context=ctx,
                client_id=str(args.client_id),
                client_type=str(args.client_type),
                pid=int(args.pid or 0),
                lease_timeout_seconds=int(args.lease_timeout_seconds or 120),
            )
            _print_json(canonical_success(result_kind="rdx.daemon.attach_client", data={"message": message, "state": state}, transport="cli") if ok else canonical_error(result_kind="rdx.daemon.attach_client", code="runtime_error", category="runtime", message=message, details={"state": state}, transport="cli"))
            return EXIT_OK if ok else EXIT_RUNTIME_ERR
        if args.daemon_cmd == "heartbeat":
            ok, message, state = heartbeat_client(
                context=ctx,
                client_id=str(args.client_id),
                pid=int(args.pid or 0),
            )
            _print_json(canonical_success(result_kind="rdx.daemon.heartbeat", data={"message": message, "state": state}, transport="cli") if ok else canonical_error(result_kind="rdx.daemon.heartbeat", code="runtime_error", category="runtime", message=message, details={"state": state}, transport="cli"))
            return EXIT_OK if ok else EXIT_RUNTIME_ERR
        if args.daemon_cmd == "detach":
            ok, message, state = detach_client(
                context=ctx,
                client_id=str(args.client_id),
            )
            _print_json(canonical_success(result_kind="rdx.daemon.detach_client", data={"message": message, "state": state}, transport="cli") if ok else canonical_error(result_kind="rdx.daemon.detach_client", code="runtime_error", category="runtime", message=message, details={"state": state}, transport="cli"))
            return EXIT_OK if ok else EXIT_RUNTIME_ERR
        if args.daemon_cmd == "cleanup":
            cleaned = cleanup_stale_daemon_states()
            _print_json(canonical_success(result_kind="rdx.daemon.cleanup", data={"cleaned": cleaned}, transport="cli"))
            return EXIT_OK

    if args.command == "context":
        if args.context_cmd == "clear":
            ok, message, details = clear_context(context=ctx)
            if not ok:
                _print_json(
                    canonical_error(
                        result_kind="rdx.context.clear",
                        code="runtime_error",
                        category="runtime",
                        message=message,
                        details=details if isinstance(details, dict) else {},
                        transport="cli",
                    ),
                )
                return EXIT_RUNTIME_ERR
            _print_json(
                canonical_success(
                    result_kind="rdx.context.clear",
                    data={"message": message, "cleared": details},
                    transport="cli",
                ),
            )
            return EXIT_OK

    if args.command == "call":
        return await _cmd_call(args)

    if args.command == "vfs":
        return await _cmd_vfs(args)

    if args.command == "capture":
        if args.capture_cmd == "open":
            return await _cmd_capture_open(args)
        if args.capture_cmd == "status":
            return _cmd_capture_status(args)

    if args.command == "diff":
        if args.diff_cmd == "pipeline":
            return await _cmd_diff_pipeline(args)
        if args.diff_cmd == "image":
            return await _cmd_diff_image(args)

    if args.command == "assert":
        if args.assert_cmd == "pipeline":
            return await _cmd_assert_pipeline(args)
        if args.assert_cmd == "image":
            return await _cmd_assert_image(args)

    raise RuntimeError("unsupported command")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    logging.getLogger().setLevel(getattr(logging, os.environ.get("RDX_LOG_LEVEL", "WARNING").upper(), logging.WARNING))
    try:
        code = asyncio.run(_main_async(args))
    except Exception as exc:  # noqa: BLE001
        _print_json(
            canonical_error(
                result_kind="rdx.cli",
                code="runtime_error",
                category="runtime",
                message=str(exc),
                transport="cli",
            ),
        )
        code = EXIT_RUNTIME_ERR
    raise SystemExit(int(code))


if __name__ == "__main__":
    main()
