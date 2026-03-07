"""RDX CLI (local-first) built on top of shared CoreEngine."""

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
    clear_session_state,
    detach_client,
    ensure_daemon,
    heartbeat_client,
    load_daemon_state,
    load_session_state,
    save_session_state,
    session_state_path,
    stop_daemon,
)
from rdx.server import dispatch_operation, runtime_shutdown, runtime_startup
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
    if key in payload:
        return payload.get(key, default)
    data = payload.get("data")
    if isinstance(data, dict):
        return data.get(key, default)
    return default


async def _direct_exec(operation: str, args: Dict[str, Any], *, remote: bool = False) -> Dict[str, Any]:
    return await dispatch_operation(operation, args, transport="cli", remote=remote)


def _daemon_exec(
    operation: str,
    args: Dict[str, Any],
    *,
    remote: bool = False,
    context: str = "default",
) -> Dict[str, Any]:
    resp = daemon_request(
        "exec",
        params={"operation": operation, "args": args, "transport": "daemon", "remote": remote},
        timeout=daemon_exec_timeout_s(operation, args),
        context=context,
    )
    if not bool(resp.get("ok")):
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        raise RuntimeError(str(err.get("message") or "daemon exec failed"))
    result = resp.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("daemon returned invalid result payload")
    return result


def _default_session_id(cli_value: Optional[str], context: str = "default") -> str:
    if cli_value:
        return str(cli_value)
    state = load_session_state(context=context)
    session_id = str(state.get("session_id") or "").strip()
    if session_id:
        return session_id
    daemon_state = load_daemon_state(context=context)
    if daemon_state:
        try:
            resp = daemon_request("get_state", params={}, context=context)
            if bool(resp.get("ok")):
                st = resp.get("result", {}).get("state", {})
                if isinstance(st, dict):
                    sid = str(st.get("session_id") or "").strip()
                    if sid:
                        return sid
        except Exception:
            pass
    raise RuntimeError("No session_id available. Use `rdx capture open --file <rdc>` first or pass --session-id.")


def _render_result(payload: Dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        _print_json(payload)
        return
    if bool(payload.get("ok")):
        print(json.dumps(payload.get("data", {}), ensure_ascii=False, indent=2))
        return
    err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    message = str(err.get("message") or payload.get("error_message") or "operation failed")
    print(f"error: {message}", file=sys.stderr)


async def _cmd_call(args: argparse.Namespace) -> int:
    call_args = _parse_args_json(args.args_json or "{}")
    if args.connect:
        payload = _daemon_exec(args.operation, call_args, remote=bool(args.remote), context=str(args.daemon_context))
    else:
        payload = await _direct_exec(args.operation, call_args, remote=bool(args.remote))
    _render_result(payload, as_json=bool(args.json))
    return EXIT_OK if bool(payload.get("ok")) else EXIT_RUNTIME_ERR


async def _cmd_capture_open(args: argparse.Namespace) -> int:
    file_path = str(Path(args.file).resolve())

    async def _exec(op: str, op_args: Dict[str, Any]) -> Dict[str, Any]:
        if args.connect:
            return _daemon_exec(op, op_args, context=str(args.daemon_context))
        return await _direct_exec(op, op_args)

    init_payload = await _exec(
        "rd.core.init",
        {
            "global_env": {"artifact_dir": str(Path(args.artifact_dir).resolve())},
            "enable_remote": True,
            "enable_app_api": True,
        },
    )
    if not bool(init_payload.get("ok")):
        _print_json(init_payload)
        return EXIT_RUNTIME_ERR

    open_file = await _exec("rd.capture.open_file", {"file_path": file_path, "read_only": True})
    if not bool(open_file.get("ok")):
        _print_json(open_file)
        return EXIT_RUNTIME_ERR
    capture_file_id = str(_extract(open_file, "capture_file_id") or "")

    open_replay = await _exec("rd.capture.open_replay", {"capture_file_id": capture_file_id, "options": {}})
    if not bool(open_replay.get("ok")):
        _print_json(open_replay)
        return EXIT_RUNTIME_ERR
    session_id = str(_extract(open_replay, "session_id") or "")

    set_frame = await _exec("rd.replay.set_frame", {"session_id": session_id, "frame_index": int(args.frame_index)})
    if not bool(set_frame.get("ok")):
        _print_json(set_frame)
        return EXIT_RUNTIME_ERR

    state = {
        "session_id": session_id,
        "capture_file_id": capture_file_id,
        "capture_path": file_path,
        "active_event_id": int(_extract(set_frame, "active_event_id", 0) or 0),
        "frame_index": int(args.frame_index),
    }
    save_session_state(state, context=str(args.daemon_context))

    if args.connect:
        try:
            daemon_request("set_state", params=state, context=str(args.daemon_context))
        except Exception:
            pass

    payload = canonical_success(
        result_kind="rdx.capture.open",
        data={
            "capture_file_id": capture_file_id,
            "session_id": session_id,
            "active_event_id": state["active_event_id"],
            "state_path": str(session_state_path(str(args.daemon_context)).resolve()),
            "persistent_session": bool(args.connect),
        },
        transport="cli",
    )
    _print_json(payload)
    return EXIT_OK


def _cmd_capture_status(args: argparse.Namespace) -> int:
    state = load_session_state(context=str(args.daemon_context))
    if not state:
        payload = canonical_error(
            result_kind="rdx.capture.status",
            code="session_not_found",
            category="not_found",
            message="no local capture session state",
            transport="cli",
        )
        _print_json(payload)
        return EXIT_RUNTIME_ERR
    payload = canonical_success(result_kind="rdx.capture.status", data={"state": state}, transport="cli")
    _print_json(payload)
    return EXIT_OK


async def _cmd_diff_pipeline(args: argparse.Namespace) -> int:
    session_id = _default_session_id(args.session_id, context=str(args.daemon_context))
    if args.connect:
        payload = _daemon_exec(
            "rd.event.diff_pipeline_state",
            {"session_id": session_id, "event_a": int(args.event_a), "event_b": int(args.event_b)},
            context=str(args.daemon_context),
        )
    else:
        payload = await _direct_exec(
            "rd.event.diff_pipeline_state",
            {"session_id": session_id, "event_a": int(args.event_a), "event_b": int(args.event_b)},
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
    if args.connect:
        payload = _daemon_exec("rd.util.diff_images", diff_args, context=str(args.daemon_context))
    else:
        payload = await _direct_exec("rd.util.diff_images", diff_args)
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
    if args.connect:
        payload = _daemon_exec(
            "rd.event.diff_pipeline_state",
            {"session_id": session_id, "event_a": int(args.event_a), "event_b": int(args.event_b)},
            context=str(args.daemon_context),
        )
    else:
        payload = await _direct_exec(
            "rd.event.diff_pipeline_state",
            {"session_id": session_id, "event_a": int(args.event_a), "event_b": int(args.event_b)},
        )
    if not bool(payload.get("ok")):
        _print_json(
            canonical_error(
                result_kind="rdx.assert.pipeline",
                code="runtime_error",
                category="runtime",
                message=str((payload.get("error") or {}).get("message") or payload.get("error_message") or "pipeline diff failed"),
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
    if args.connect:
        payload = _daemon_exec("rd.util.diff_images", diff_args, context=str(args.daemon_context))
    else:
        payload = await _direct_exec("rd.util.diff_images", diff_args)
    if not bool(payload.get("ok")):
        _print_json(
            canonical_error(
                result_kind="rdx.assert.image",
                code="runtime_error",
                category="runtime",
                message=str((payload.get("error") or {}).get("message") or payload.get("error_message") or "image diff failed"),
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
    parser = argparse.ArgumentParser(prog="rdx", description="RDX local-first CLI")
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
    p_daemon_attach.add_argument("--client-type", default="cli-shell")
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
    p_call.add_argument("--json", action="store_true")
    p_call.add_argument("--remote", action="store_true")
    p_call.add_argument("--connect", action="store_true")

    p_capture = sub.add_parser("capture", help="Capture session helpers")
    s_capture = p_capture.add_subparsers(dest="capture_cmd", required=True)
    p_capture_open = s_capture.add_parser("open")
    p_capture_open.add_argument("--file", required=True)
    p_capture_open.add_argument("--frame-index", type=int, default=0)
    p_capture_open.add_argument("--artifact-dir", default=str(artifacts_dir().resolve()))
    p_capture_open.add_argument("--connect", action="store_true", help="Open capture in daemon session for cross-command reuse")
    s_capture.add_parser("status")

    p_diff = sub.add_parser("diff", help="Diff commands")
    s_diff = p_diff.add_subparsers(dest="diff_cmd", required=True)
    p_diff_pipeline = s_diff.add_parser("pipeline")
    p_diff_pipeline.add_argument("--session-id", default=None)
    p_diff_pipeline.add_argument("--event-a", required=True, type=int)
    p_diff_pipeline.add_argument("--event-b", required=True, type=int)
    p_diff_pipeline.add_argument("--fail-on-diff", action="store_true", help="Return exit code 1 if any diff exists")
    p_diff_pipeline.add_argument("--connect", action="store_true")
    p_diff_image = s_diff.add_parser("image")
    p_diff_image.add_argument("--image-a", required=True)
    p_diff_image.add_argument("--image-b", required=True)
    p_diff_image.add_argument("--out", default=None)
    p_diff_image.add_argument("--threshold", type=float, default=None)
    p_diff_image.add_argument("--connect", action="store_true")

    p_assert = sub.add_parser("assert", help="Assertion commands")
    s_assert = p_assert.add_subparsers(dest="assert_cmd", required=True)
    p_assert_pipeline = s_assert.add_parser("pipeline")
    p_assert_pipeline.add_argument("--session-id", default=None)
    p_assert_pipeline.add_argument("--event-a", required=True, type=int)
    p_assert_pipeline.add_argument("--event-b", required=True, type=int)
    p_assert_pipeline.add_argument("--max-changes", type=int, default=0)
    p_assert_pipeline.add_argument("--connect", action="store_true")
    p_assert_image = s_assert.add_parser("image")
    p_assert_image.add_argument("--image-a", required=True)
    p_assert_image.add_argument("--image-b", required=True)
    p_assert_image.add_argument("--out", default=None)
    p_assert_image.add_argument("--mse-max", type=float, default=None)
    p_assert_image.add_argument("--max-abs-max", type=float, default=None)
    p_assert_image.add_argument("--psnr-min", type=float, default=None)
    p_assert_image.add_argument("--connect", action="store_true")

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
            _print_json(canonical_success(result_kind="rdx.daemon.start", data={"message": message, "state": state}, transport="cli") if ok else canonical_error(result_kind="rdx.daemon.start", code="runtime_error", category="runtime", message=message, transport="cli"))
            return EXIT_OK if ok else EXIT_RUNTIME_ERR
        if args.daemon_cmd == "stop":
            ok, message = stop_daemon(context=ctx)
            _print_json(canonical_success(result_kind="rdx.daemon.stop", data={"message": message}, transport="cli") if ok else canonical_error(result_kind="rdx.daemon.stop", code="runtime_error", category="runtime", message=message, transport="cli"))
            return EXIT_OK if ok else EXIT_RUNTIME_ERR
        if args.daemon_cmd == "status":
            st = load_daemon_state(context=ctx)
            if not st:
                cleanup_stale_daemon_states(context=ctx)
                st = load_daemon_state(context=ctx)
            if not st:
                _print_json(canonical_error(result_kind="rdx.daemon.status", code="not_found", category="not_found", message="no active daemon", transport="cli"))
                return EXIT_RUNTIME_ERR
            try:
                resp = daemon_request("status", params={}, context=ctx, state=st)
            except Exception as exc:  # noqa: BLE001
                cleaned = cleanup_stale_daemon_states(context=ctx)
                refreshed = load_daemon_state(context=ctx)
                if not refreshed:
                    _print_json(canonical_error(result_kind="rdx.daemon.status", code="not_found", category="not_found", message="no active daemon", details={"state": st, "cleaned": cleaned}, transport="cli"))
                    return EXIT_RUNTIME_ERR
                _print_json(canonical_error(result_kind="rdx.daemon.status", code="runtime_error", category="runtime", message=str(exc), details={"state": refreshed, "cleaned": cleaned}, transport="cli"))
                return EXIT_RUNTIME_ERR
            if bool(resp.get("ok")):
                result = resp.get("result", {})
                state = result.get("state") if isinstance(result, dict) else {}
                if isinstance(state, dict):
                    st = state
                _print_json(canonical_success(result_kind="rdx.daemon.status", data={"daemon": result, "state": st}, transport="cli"))
                return EXIT_OK
            err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
            _print_json(canonical_error(result_kind="rdx.daemon.status", code=str(err.get("code") or "runtime_error"), category="runtime", message=str(err.get("message") or "daemon status failed"), details={"state": st}, transport="cli"))
            return EXIT_RUNTIME_ERR
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
            clear_session_state(context=ctx)
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


def _needs_local_runtime(args: argparse.Namespace) -> bool:
    if args.command == "daemon":
        return False
    if args.command == "context":
        return False
    if args.command == "capture" and args.capture_cmd == "status":
        return False
    if bool(getattr(args, "connect", False)):
        return False
    return True


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Keep CLI output stable for machine consumers unless caller asks for more logs.
    logging.getLogger().setLevel(getattr(logging, os.environ.get("RDX_LOG_LEVEL", "WARNING").upper(), logging.WARNING))

    use_local_runtime = _needs_local_runtime(args)
    if use_local_runtime:
        asyncio.run(runtime_startup())
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
    finally:
        if use_local_runtime:
            asyncio.run(runtime_shutdown())
    raise SystemExit(int(code))


if __name__ == "__main__":
    main()
