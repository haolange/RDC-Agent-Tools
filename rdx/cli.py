"""RDX CLI adapter backed by the daemon-owned runtime."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from rdx.core.assert_service import AssertService
from rdx import __version__ as TOOL_VERSION
from rdx.core.contracts import SCHEMA_VERSION, canonical_error, canonical_success
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
from rdx.io_utils import safe_json_text, safe_stream_write
from rdx.python_runtime import current_python_runtime_details, validate_bundled_python_layout
from rdx.runtime_catalog import load_tool_catalog, tool_catalog_path
from rdx.runtime_paths import (
    artifacts_dir,
    binaries_root,
    bundled_python_executable,
    cli_runtime_dir,
    ensure_runtime_dirs,
    logs_dir,
    pymodules_dir,
    runtime_root,
    tools_root,
)
from rdx.runtime_requirements import missing_dependencies
from rdx.timeout_policy import daemon_exec_timeout_s

EXIT_OK = 0
EXIT_ASSERT_FAIL = 1
EXIT_RUNTIME_ERR = 1
EXIT_USAGE_ERR = 2


def _print_json(payload: Dict[str, Any]) -> None:
    safe_stream_write(safe_json_text(payload, indent=2) + "\n", sys.stdout)


def _write_stdout(text: str) -> None:
    safe_stream_write(text + "\n", sys.stdout)


def _print_launcher_help() -> None:
    for line in (
        "usage: rdx [--json] [--daemon-context <id>] <command> ...",
        "commands:",
        "  version",
        "  doctor",
        "  tools list|search",
        "  daemon start|stop|status",
        "  context status|update|list|clear",
        "  session preview on|off|status",
        "  completion powershell|bash|zsh|fish",
        "  call <operation> [--args-json ... | --args-file ...] [--format json|tsv] [--remote]",
        "  capture open|status",
        "  vfs ls|cat|tree|resolve",
        "  event list|show",
        "  pipeline show|section",
        "  shader source|disasm|constants",
        "  export screenshot|texture|buffer|mesh",
        "  pixel value|history",
        "  resource list|show|usage",
        "  diff pipeline|image",
        "  assert pipeline|image",
        "",
        "examples:",
        "  rdx --version",
        "  rdx version --json",
        "  rdx --json doctor",
        "  rdx completion powershell",
        "  rdx tools search pipeline --json",
        "  rdx daemon start --daemon-context local",
        "  rdx context status --daemon-context local --json",
        "  rdx context update --daemon-context local --key notes --value triaged --json",
        "  rdx context clear --daemon-context local",
        "  rdx capture open --file D:\\path\\capture.rdc --frame-index 0 --preview",
        "  rdx session preview on",
        "  rdx call rd.session.get_context --args-file .\\args.json --format json",
        "  rdx vfs ls --path / --format tsv",
        "  rdx event list --format tsv",
        "  rdx pipeline show --event-id 42",
        "  rdx shader source --event-id 42 --stage ps",
        "  rdx export screenshot --event-id 42 --out .\\frame.png",
    ):
        _write_stdout(line)


def _exception_error_payload(result_kind: str, exc: Exception, *, transport: str = "cli") -> Dict[str, Any]:
    code = str(getattr(exc, "code", "") or "runtime_error")
    category = str(getattr(exc, "category", "") or "runtime")
    details = getattr(exc, "details", {})
    return canonical_error(
        result_kind=result_kind,
        code=code,
        category=category,
        message=str(exc),
        details=details if isinstance(details, dict) else {},
        transport=transport,
    )


def _parse_json_object(raw: str, *, source: str) -> Dict[str, Any]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{source} must be a JSON object")
    return parsed


def _windows_command_line() -> str:
    if os.name != "nt":
        return ""
    try:
        get_command_line = ctypes.windll.kernel32.GetCommandLineW
        get_command_line.restype = ctypes.c_wchar_p
        return str(get_command_line() or "")
    except Exception:
        return ""


def _extract_raw_args_json_from_command_line(command_line: str) -> str:
    raw = str(command_line or "")
    if not raw:
        return ""

    match = re.search(r"(?:^|\s)--args-json\b", raw)
    if match is None:
        return ""

    idx = match.end()
    while idx < len(raw) and raw[idx].isspace():
        idx += 1
    if idx < len(raw) and raw[idx] == '"':
        idx += 1
    if idx >= len(raw) or raw[idx] not in "{[":
        return ""

    opening = raw[idx]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    start = idx
    while idx < len(raw):
        ch = raw[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == opening:
                depth += 1
            elif ch == closing:
                depth -= 1
                if depth == 0:
                    return raw[start : idx + 1]
        idx += 1
    return ""


def _recover_args_json_from_command_line() -> str:
    return _extract_raw_args_json_from_command_line(_windows_command_line())


def _load_call_args(*, args_json: Optional[str] = None, args_file: Optional[str] = None) -> Dict[str, Any]:
    raw_json = str(args_json or "")
    raw_file = str(args_file or "").strip()
    has_json = bool(raw_json.strip())
    has_file = bool(raw_file)

    if has_json and has_file:
        raise ValueError("--args-json and --args-file are mutually exclusive")
    if has_file:
        path = Path(raw_file).expanduser()
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"--args-file could not be read: {path}") from exc
        try:
            return _parse_json_object(raw, source="--args-file")
        except json.JSONDecodeError as exc:
            raise ValueError(f"--args-file contains invalid JSON: {exc.msg}") from exc
    if has_json:
        try:
            return _parse_json_object(raw_json, source="--args-json")
        except json.JSONDecodeError as exc:
            recovered = _recover_args_json_from_command_line()
            if recovered and recovered != raw_json:
                try:
                    return _parse_json_object(recovered, source="--args-json")
                except json.JSONDecodeError:
                    pass
            raise ValueError(
                "--args-json contains invalid JSON: "
                f"{exc.msg}. Use --args-file args.json for multiline shader source. "
                "Example: rdx call rd.shader.edit_and_replace --args-file args.json --format json"
            ) from exc
    return {}


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
    except Exception as exc:
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
            code=str(getattr(exc, "code", "") or "runtime_error"),
            category=str(getattr(exc, "category", "") or "runtime"),
            message="daemon status failed",
            details={
                "state": refreshed,
                "cleaned": cleaned,
                **(getattr(exc, "details", {}) if isinstance(getattr(exc, "details", {}), dict) else {}),
            },
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
        safe_stream_write(text + "\n", sys.stdout)
        return
    columns = tabular.get("columns")
    rows = tabular.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise RuntimeError("tabular projection is missing columns/rows")
    _write_stdout("\t".join(str(col) for col in columns))
    for row in rows:
        if not isinstance(row, list):
            raise RuntimeError("tabular row must be a list")
        _write_stdout("\t".join("" if item is None else str(item) for item in row))


def _tabular_projection_error_payload(payload: Dict[str, Any], message: str) -> Dict[str, Any]:
    result_kind = str(payload.get("result_kind") or "rdx.cli")
    return canonical_error(
        result_kind=result_kind,
        code="tabular_projection_missing",
        category="validation",
        message=message,
        details={
            "requested_format": "tsv",
            "source_result_kind": result_kind,
            "recovery_hint": "Use --format json unless the command documents a tabular projection.",
        },
        transport="cli",
    )


def _render_result(payload: Dict[str, Any], *, output_format: str = "json") -> bool:
    if output_format == "tsv" and bool(payload.get("ok")):
        try:
            _render_tabular(payload)
        except RuntimeError as exc:
            _print_json(_tabular_projection_error_payload(payload, str(exc)))
            return False
        return True
    _print_json(payload)
    return bool(payload.get("ok"))


def _tool_summary(tool: Dict[str, Any]) -> Dict[str, Any]:
    name = str(tool.get("name") or "")
    namespace = name.split(".")[1] if name.startswith("rd.") and len(name.split(".")) > 1 else ""
    return {
        "name": name,
        "namespace": namespace,
        "group": str(tool.get("group") or ""),
        "description": str(tool.get("description") or ""),
        "param_names": list(tool.get("param_names") or []),
        "prerequisites": list(tool.get("prerequisites") or []),
    }


def _cmd_doctor(args: argparse.Namespace) -> int:
    context = str(getattr(args, "daemon_context", "default") or "default")
    root = tools_root().resolve()
    ensure_runtime_dirs()

    dependencies_missing = missing_dependencies()
    python_ok, python_failures, python_details = validate_bundled_python_layout()

    renderdoc_dll = binaries_root() / "renderdoc.dll"
    renderdoc_json = binaries_root() / "renderdoc.json"
    renderdoc_pyd = pymodules_dir() / "renderdoc.pyd"
    renderdoc_failures = [
        str(path)
        for path in (renderdoc_dll, renderdoc_json, renderdoc_pyd)
        if not path.is_file()
    ]

    catalog_error = ""
    catalog_count = 0
    try:
        catalog = load_tool_catalog()
        catalog_count = len(catalog)
    except Exception as exc:  # noqa: BLE001
        catalog_error = str(exc)

    try:
        daemon_status = _daemon_status_payload(context)
    except Exception as exc:  # noqa: BLE001
        daemon_status = canonical_error(
            result_kind="rdx.daemon.status",
            code=str(getattr(exc, "code", "") or "runtime_error"),
            category=str(getattr(exc, "category", "") or "runtime"),
            message=str(exc),
            details=getattr(exc, "details", {}) if isinstance(getattr(exc, "details", {}), dict) else {},
            transport="cli",
        )

    launcher_paths = {
        "windows_bat": str(root / "rdx.bat"),
        "posix_shell": str(root / "bin" / "rdx"),
        "python_cli": str(root / "cli" / "run_cli.py"),
    }
    spirv_as = shutil.which("spirv-as") or shutil.which("spirv-as.exe")
    spirv_dis = shutil.which("spirv-dis") or shutil.which("spirv-dis.exe")
    details = {
        "tools_root": str(root),
        "context_id": context,
        "python": {
            "current": current_python_runtime_details(),
            "bundled": python_details,
            "bundled_python_ok": python_ok,
            "bundled_python_failures": python_failures,
            "bundled_python_executable": str(bundled_python_executable()),
        },
        "dependencies": {
            "missing": dependencies_missing,
            "auth_required": False,
        },
        "renderdoc": {
            "layout_ok": not renderdoc_failures,
            "failures": renderdoc_failures,
            "renderdoc_dll": str(renderdoc_dll),
            "renderdoc_json": str(renderdoc_json),
            "renderdoc_pyd": str(renderdoc_pyd),
        },
        "shader_tools": {
            "spirv_as": {
                "available": bool(spirv_as),
                "path": str(spirv_as or ""),
                "required_for": [
                    "rd.shader.edit_and_replace raw SPIR-V ASM when RenderDoc only accepts SPIRV binary encoding",
                ],
            },
            "spirv_dis": {
                "available": bool(spirv_dis),
                "path": str(spirv_dis or ""),
                "required_for": [
                    "rd.shader.get_disassembly raw SPIR-V ASM fallback when RenderDoc does not expose a raw ASM target",
                ],
            },
        },
        "catalog": {
            "path": str(tool_catalog_path()),
            "tool_count": catalog_count,
            "error": catalog_error,
        },
        "runtime_dirs": {
            "runtime_root": str(runtime_root()),
            "cli_runtime_dir": str(cli_runtime_dir()),
            "artifacts_dir": str(artifacts_dir()),
            "logs_dir": str(logs_dir()),
        },
        "launchers": {
            **launcher_paths,
            "windows_bat_exists": (root / "rdx.bat").is_file(),
            "posix_shell_exists": (root / "bin" / "rdx").is_file(),
            "python_cli_exists": (root / "cli" / "run_cli.py").is_file(),
        },
        "daemon": daemon_status,
    }
    ok = (
        not dependencies_missing
        and python_ok
        and not renderdoc_failures
        and not catalog_error
        and (root / "rdx.bat").is_file()
        and (root / "bin" / "rdx").is_file()
        and (root / "cli" / "run_cli.py").is_file()
    )
    if ok:
        _print_json(canonical_success(result_kind="rdx.doctor", data=details, transport="cli"))
        return EXIT_OK
    _print_json(
        canonical_error(
            result_kind="rdx.doctor",
            code="setup_incomplete",
            category="environment",
            message="rdx-tools setup is incomplete",
            details=details,
            transport="cli",
        ),
    )
    return EXIT_RUNTIME_ERR


def _version_payload() -> Dict[str, Any]:
    root = tools_root().resolve()
    return canonical_success(
        result_kind="rdx.version",
        data={
            "tool_version": TOOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "platform": "windows-x64" if os.name == "nt" else sys.platform,
            "tools_root": str(root),
            "public_commands": ["rdx"],
            "entrypoints": {
                "windows_bat": str(root / "rdx.bat"),
                "posix_shell": str(root / "bin" / "rdx"),
                "python_cli": str(root / "cli" / "run_cli.py"),
            },
            "compatibility": {
                "stability": "1.x",
                "json_envelope": "stable",
            },
        },
        transport="cli",
    )


def _cmd_version(args: argparse.Namespace) -> int:
    if bool(getattr(args, "json", False)):
        _print_json(_version_payload())
    else:
        _write_stdout(f"rdx {TOOL_VERSION}")
    return EXIT_OK


def _completion_words() -> list[str]:
    static_words = [
        "doctor",
        "version",
        "tools",
        "list",
        "search",
        "daemon",
        "start",
        "stop",
        "status",
        "context",
        "clear",
        "session",
        "preview",
        "on",
        "off",
        "capture",
        "open",
        "call",
        "vfs",
        "event",
        "show",
        "pipeline",
        "section",
        "shader",
        "source",
        "disasm",
        "constants",
        "export",
        "screenshot",
        "texture",
        "buffer",
        "mesh",
        "pixel",
        "value",
        "history",
        "resource",
        "ls",
        "cat",
        "tree",
        "resolve",
        "diff",
        "pipeline",
        "image",
        "assert",
        "completion",
        "powershell",
        "bash",
        "zsh",
        "fish",
        "--json",
        "--daemon-context",
        "--version",
        "--help",
        "--file",
        "--frame-index",
        "--args-json",
        "--args-file",
        "--format",
        "--remote",
        "--session-id",
        "--event-id",
        "--stage",
        "--resource-id",
        "--out",
        "--slot",
        "--x",
        "--y",
    ]
    try:
        tool_names = [str(item.get("name") or "") for item in load_tool_catalog()]
    except Exception:
        tool_names = []
    return sorted({word for word in [*static_words, *tool_names] if word})


def _completion_script(shell: str) -> str:
    words = _completion_words()
    if shell == "powershell":
        quoted = ", ".join("'" + word.replace("'", "''") + "'" for word in words)
        return "\n".join(
            [
                "Register-ArgumentCompleter -Native -CommandName rdx -ScriptBlock {",
                "  param($wordToComplete, $commandAst, $cursorPosition)",
                f"  $words = @({quoted})",
                "  $words | Where-Object { $_ -like \"$wordToComplete*\" } | ForEach-Object {",
                "    [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)",
                "  }",
                "}",
                "",
            ]
        )
    if shell == "bash":
        joined = " ".join(words)
        return "\n".join(
            [
                "_rdx_complete() {",
                "  local cur=\"${COMP_WORDS[COMP_CWORD]}\"",
                f"  COMPREPLY=( $(compgen -W \"{joined}\" -- \"$cur\") )",
                "}",
                "complete -F _rdx_complete rdx",
                "",
            ]
        )
    if shell == "zsh":
        joined = " ".join(words)
        return "\n".join(
            [
                "#compdef rdx",
                "_rdx() {",
                f"  compadd -- {joined}",
                "}",
                "_rdx \"$@\"",
                "",
            ]
        )
    if shell == "fish":
        return "".join(f"complete -c rdx -f -a '{word}'\n" for word in words)
    raise ValueError(f"unsupported completion shell: {shell}")


def _cmd_completion(args: argparse.Namespace) -> int:
    _write_stdout(_completion_script(str(args.shell)))
    return EXIT_OK


def _cmd_tools_list(args: argparse.Namespace) -> int:
    tools = [_tool_summary(tool) for tool in load_tool_catalog()]
    namespace = str(getattr(args, "namespace", "") or "").strip()
    if namespace:
        tools = [tool for tool in tools if tool.get("namespace") == namespace]
    limit = int(getattr(args, "limit", 0) or 0)
    if limit > 0:
        tools = tools[:limit]
    payload = canonical_success(
        result_kind="rdx.tools.list",
        data={
            "tool_count": len(tools),
            "tools": tools,
        },
        transport="cli",
    )
    _print_json(payload)
    return EXIT_OK


def _cmd_tools_search(args: argparse.Namespace) -> int:
    query = str(getattr(args, "query", "") or "").strip().lower()
    if not query:
        _print_json(
            canonical_error(
                result_kind="rdx.tools.search",
                code="query_required",
                category="validation",
                message="tools search requires a query",
                transport="cli",
            ),
        )
        return EXIT_RUNTIME_ERR
    results = []
    for tool in (_tool_summary(item) for item in load_tool_catalog()):
        haystack = " ".join(
            str(tool.get(key) or "")
            for key in ("name", "namespace", "group", "description")
        ).lower()
        if query in haystack:
            results.append(tool)
    limit = int(getattr(args, "limit", 20) or 20)
    if limit > 0:
        results = results[:limit]
    payload = canonical_success(
        result_kind="rdx.tools.search",
        data={
            "query": query,
            "tool_count": len(results),
            "tools": results,
        },
        transport="cli",
    )
    _print_json(payload)
    return EXIT_OK


def _extract_error_triplet(payload: Dict[str, Any]) -> tuple[str, str, str]:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    return (
        str(error.get("code") or "runtime_error"),
        str(error.get("category") or "runtime"),
        str(error.get("message") or "runtime error"),
    )


def _safe_capture_open_context_snapshot(context: str) -> Dict[str, Any]:
    try:
        payload = _daemon_exec("rd.session.get_context", {}, context=context)
    except Exception as exc:  # noqa: BLE001
        nested = _exception_error_payload("rd.session.get_context", exc, transport="cli")
        return {"ok": False, "error": nested.get("error"), "meta": nested.get("meta", {})}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {"ok": bool(payload.get("ok")), "data": data, "error": payload.get("error"), "meta": payload.get("meta", {})}


def _parse_context_value(raw: str) -> Any:
    text = str(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _cmd_context_status(args: argparse.Namespace) -> int:
    context = str(getattr(args, "daemon_context", "default") or "default")
    payload = _daemon_exec("rd.session.get_context", {}, context=context)
    _print_json(payload)
    return EXIT_OK if bool(payload.get("ok")) else EXIT_RUNTIME_ERR


def _cmd_context_update(args: argparse.Namespace) -> int:
    context = str(getattr(args, "daemon_context", "default") or "default")
    call_args = {"key": str(args.key), "value": _parse_context_value(str(args.value))}
    payload = _daemon_exec("rd.session.update_context", call_args, context=context)
    _print_json(payload)
    return EXIT_OK if bool(payload.get("ok")) else EXIT_RUNTIME_ERR


def _cmd_context_list(args: argparse.Namespace) -> int:
    context = str(getattr(args, "daemon_context", "default") or "default")
    payload = _daemon_exec("rd.session.list_contexts", {}, context=context)
    _print_json(payload)
    return EXIT_OK if bool(payload.get("ok")) else EXIT_RUNTIME_ERR


def _session_required_error_payload(result_kind: str, context: str, message: str) -> Dict[str, Any]:
    return canonical_error(
        result_kind=result_kind,
        code="session_required",
        category="validation",
        message=message,
        details={
            "context_id": context,
            "requires_session": True,
            "recovery_hint": "Open a capture with `rdx capture open --file <rdc>` or pass --session-id.",
        },
        transport="cli",
    )


def _capture_open_error_payload(
    *,
    step: str,
    context: str,
    file_path: str,
    capture_file_id: str = "",
    session_id: str = "",
    active_event_id: int = 0,
    source_payload: Optional[Dict[str, Any]] = None,
    source_exception: Optional[Exception] = None,
) -> Dict[str, Any]:
    if isinstance(source_payload, dict) and not bool(source_payload.get("ok")):
        code, category, message = _extract_error_triplet(source_payload)
        source_details = dict((source_payload.get("error") or {}).get("details") or {})
    elif source_exception is not None:
        nested = _exception_error_payload("rdx.capture.open", source_exception, transport="cli")
        code, category, message = _extract_error_triplet(nested)
        source_details = dict((nested.get("error") or {}).get("details") or {})
    else:
        code, category, message = ("runtime_error", "runtime", f"{step} failed")
        source_details = {}
    try:
        daemon_status = _daemon_status_payload(context)
    except Exception as exc:  # noqa: BLE001
        daemon_status = _exception_error_payload("rdx.daemon.status", exc, transport="cli")
    daemon_state = daemon_status.get("data", {}).get("state", {}) if isinstance(daemon_status.get("data"), dict) else {}
    context_snapshot = _safe_capture_open_context_snapshot(context)
    details = {
        "failed_step": step,
        "context_id": context,
        "file_path": file_path,
        "capture_file_id": str(capture_file_id or ""),
        "session_id": str(session_id or ""),
        "active_event_id": int(active_event_id or 0),
        "step_payload": source_payload if isinstance(source_payload, dict) else {},
        "source_error_details": source_details,
        "daemon_state": daemon_state if isinstance(daemon_state, dict) else {},
        "context_snapshot": context_snapshot,
        "recovery_hint": "Inspect failed_step; if the context still reports an active operation or stale handles, run `rdx context clear` before retrying.",
    }
    return canonical_error(
        result_kind="rdx.capture.open",
        code=code,
        category=category,
        message=f"capture open failed during {step}: {message}",
        details=details,
        transport="cli",
    )


async def _cmd_call(args: argparse.Namespace) -> int:
    call_args = _tabular_request(
        str(args.format),
        _load_call_args(
            args_json=getattr(args, "args_json", None),
            args_file=getattr(args, "args_file", None),
        ),
    )
    payload = _daemon_exec(args.operation, call_args, remote=bool(args.remote), context=str(args.daemon_context))
    return EXIT_OK if _render_result(payload, output_format=str(args.format)) else EXIT_RUNTIME_ERR


async def _cmd_vfs(args: argparse.Namespace) -> int:
    op = f"rd.vfs.{args.vfs_cmd}"
    call_args: Dict[str, Any] = {"path": str(args.path or "/")}
    if getattr(args, "session_id", None):
        call_args["session_id"] = str(args.session_id)
    if args.vfs_cmd == "tree":
        call_args["depth"] = int(args.depth)
        call_args["max_nodes"] = int(args.max_nodes)
    payload = _daemon_exec(op, _tabular_request(str(args.format), call_args), context=str(args.daemon_context))
    return EXIT_OK if _render_result(payload, output_format=str(args.format)) else EXIT_RUNTIME_ERR


_FACADE_TSV_COMMANDS = {("event", "list"), ("resource", "list")}


def _facade_subcommand(args: argparse.Namespace) -> str:
    for attr in (
        "event_cmd",
        "pipeline_cmd",
        "shader_cmd",
        "export_cmd",
        "pixel_cmd",
        "resource_cmd",
    ):
        value = getattr(args, attr, None)
        if value:
            return str(value)
    return ""


def _facade_result_kind(args: argparse.Namespace) -> str:
    subcommand = _facade_subcommand(args)
    return f"rdx.{args.command}.{subcommand}" if subcommand else f"rdx.{args.command}"


def _facade_projection_not_supported_payload(args: argparse.Namespace, context: str) -> Dict[str, Any]:
    return canonical_error(
        result_kind=_facade_result_kind(args),
        code="projection_not_supported",
        category="validation",
        message="--format tsv is only supported by facade list/projection commands.",
        details={
            "context_id": context,
            "requested_format": "tsv",
            "command": str(args.command),
            "subcommand": _facade_subcommand(args),
            "supported_tsv_commands": ["event list", "resource list", "vfs ls", "call with a tabular tool projection"],
            "recovery_hint": "Use --format json for nested pipeline, shader, export, pixel, and resource-detail payloads.",
        },
        transport="cli",
    )


def _facade_session_id(args: argparse.Namespace, context: str) -> str:
    return _default_session_id(getattr(args, "session_id", None), context=context)


def _add_optional_event_id(target: Dict[str, Any], value: Any) -> None:
    if value is not None:
        target["event_id"] = int(value)


def _facade_request(args: argparse.Namespace, session_id: str) -> tuple[str, Dict[str, Any]]:
    command = str(args.command)
    subcommand = _facade_subcommand(args)

    if command == "event":
        if subcommand == "list":
            return "rd.event.get_actions", {"session_id": session_id}
        if subcommand == "show":
            return "rd.event.get_action_details", {"session_id": session_id, "event_id": int(args.event_id)}

    if command == "pipeline":
        call_args: Dict[str, Any] = {"session_id": session_id}
        _add_optional_event_id(call_args, getattr(args, "event_id", None))
        if subcommand == "show":
            call_args["context_id"] = str(args.daemon_context)
            return "rd.pipeline.get_state_summary", call_args
        if subcommand == "section":
            call_args["stage"] = str(args.stage)
            return "rd.pipeline.get_stage_state", call_args

    if command == "shader":
        call_args = {"session_id": session_id, "stage": str(args.stage)}
        _add_optional_event_id(call_args, getattr(args, "event_id", None))
        if subcommand == "source":
            return "rd.shader.get_source", call_args
        if subcommand == "disasm":
            return "rd.shader.get_disassembly", call_args
        if subcommand == "constants":
            call_args["slot"] = int(args.slot)
            return "rd.shader.get_constant_buffer_contents", call_args

    if command == "export":
        call_args = {"session_id": session_id, "output_path": str(Path(args.out).resolve())}
        _add_optional_event_id(call_args, getattr(args, "event_id", None))
        if subcommand == "screenshot":
            return "rd.export.screenshot", call_args
        if subcommand == "texture":
            call_args["texture_id"] = str(args.resource_id)
            return "rd.export.texture", call_args
        if subcommand == "buffer":
            call_args["buffer_id"] = str(args.resource_id)
            return "rd.export.buffer", call_args
        if subcommand == "mesh":
            return "rd.export.mesh", call_args

    if command == "pixel":
        call_args = {
            "session_id": session_id,
            "texture_id": str(args.resource_id),
            "x": int(args.x),
            "y": int(args.y),
        }
        _add_optional_event_id(call_args, getattr(args, "event_id", None))
        if subcommand == "value":
            return "rd.texture.get_pixel_value", call_args
        if subcommand == "history":
            return "rd.texture.get_pixel_history", call_args

    if command == "resource":
        if subcommand == "list":
            return "rd.resource.list_all", {"session_id": session_id}
        call_args = {"session_id": session_id, "resource_id": str(args.resource_id)}
        if subcommand == "show":
            return "rd.resource.get_details", call_args
        if subcommand == "usage":
            return "rd.resource.get_usage", call_args

    raise RuntimeError(f"unsupported facade command: {command} {subcommand}".strip())


async def _cmd_facade(args: argparse.Namespace) -> int:
    context = str(args.daemon_context)
    output_format = str(getattr(args, "format", "json") or "json")
    try:
        session_id = _facade_session_id(args, context)
    except RuntimeError as exc:
        _print_json(_session_required_error_payload(_facade_result_kind(args), context, str(exc)))
        return EXIT_RUNTIME_ERR

    op, call_args = _facade_request(args, session_id)
    if output_format == "tsv" and (str(args.command), _facade_subcommand(args)) not in _FACADE_TSV_COMMANDS:
        _print_json(_facade_projection_not_supported_payload(args, context))
        return EXIT_RUNTIME_ERR

    payload = _daemon_exec(op, _tabular_request(output_format, call_args), context=context)
    return EXIT_OK if _render_result(payload, output_format=output_format) else EXIT_RUNTIME_ERR


async def _cmd_capture_open(args: argparse.Namespace) -> int:
    file_path = str(Path(args.file).resolve())
    context = str(args.daemon_context)
    capture_file_id = ""
    session_id = ""
    active_event_id = 0

    def _print_capture_open_error(
        step: str,
        *,
        source_payload: Optional[Dict[str, Any]] = None,
        source_exception: Optional[Exception] = None,
    ) -> int:
        _print_json(
            _capture_open_error_payload(
                step=step,
                context=context,
                file_path=file_path,
                capture_file_id=capture_file_id,
                session_id=session_id,
                active_event_id=active_event_id,
                source_payload=source_payload,
                source_exception=source_exception,
            )
        )
        return EXIT_RUNTIME_ERR

    try:
        init_payload = _daemon_exec(
            "rd.core.init",
            {
                "global_env": {"artifact_dir": str(Path(args.artifact_dir).resolve())},
                "enable_remote": True,
            },
            context=context,
        )
    except Exception as exc:  # noqa: BLE001
        return _print_capture_open_error("init", source_exception=exc)
    if not bool(init_payload.get("ok")):
        return _print_capture_open_error("init", source_payload=init_payload)

    try:
        open_file = _daemon_exec("rd.capture.open_file", {"file_path": file_path, "read_only": True}, context=context)
    except Exception as exc:  # noqa: BLE001
        return _print_capture_open_error("open_file", source_exception=exc)
    if not bool(open_file.get("ok")):
        return _print_capture_open_error("open_file", source_payload=open_file)
    capture_file_id = str(_extract(open_file, "capture_file_id") or "")

    try:
        open_replay = _daemon_exec(
            "rd.capture.open_replay",
            {"capture_file_id": capture_file_id, "options": {}},
            context=context,
        )
    except Exception as exc:  # noqa: BLE001
        return _print_capture_open_error("open_replay", source_exception=exc)
    if not bool(open_replay.get("ok")):
        return _print_capture_open_error("open_replay", source_payload=open_replay)
    session_id = str(_extract(open_replay, "session_id") or "")
    active_event_id = int(_extract(open_replay, "active_event_id", 0) or 0)

    try:
        set_frame = _daemon_exec(
            "rd.replay.set_frame",
            {"session_id": session_id, "frame_index": int(args.frame_index)},
            context=context,
        )
    except Exception as exc:  # noqa: BLE001
        return _print_capture_open_error("set_frame", source_exception=exc)
    if not bool(set_frame.get("ok")):
        return _print_capture_open_error("set_frame", source_payload=set_frame)
    active_event_id = int(_extract(set_frame, "active_event_id", active_event_id) or active_event_id or 0)

    try:
        context_payload = _daemon_exec("rd.session.get_context", {}, context=context)
    except Exception as exc:  # noqa: BLE001
        return _print_capture_open_error("get_context", source_exception=exc)
    if not bool(context_payload.get("ok")):
        return _print_capture_open_error("get_context", source_payload=context_payload)
    if bool(getattr(args, "preview", False)):
        try:
            preview_payload = _daemon_exec("rd.session.open_preview", {}, context=context)
        except Exception as exc:  # noqa: BLE001
            return _print_capture_open_error("open_preview", source_exception=exc)
        if not bool(preview_payload.get("ok")):
            return _print_capture_open_error("open_preview", source_payload=preview_payload)
        try:
            context_payload = _daemon_exec("rd.session.get_context", {}, context=context)
        except Exception as exc:  # noqa: BLE001
            return _print_capture_open_error("get_context_after_preview", source_exception=exc)
        if not bool(context_payload.get("ok")):
            return _print_capture_open_error("get_context_after_preview", source_payload=context_payload)
    runtime_snapshot = context_payload.get("data", {}).get("runtime", {}) if isinstance(context_payload.get("data"), dict) else {}
    context_data = context_payload.get("data") if isinstance(context_payload.get("data"), dict) else {}
    recovery_status = str(_extract(open_replay, "recovery_status", "") or "")
    if not recovery_status and isinstance(runtime_snapshot, dict):
        recovery_status = str(runtime_snapshot.get("recovery_status") or "")
    if not recovery_status and isinstance(context_data, dict):
        current_session_id = str(context_data.get("current_session_id") or session_id)
        for item in context_data.get("sessions", []) or []:
            if isinstance(item, dict) and str(item.get("session_id") or "") == current_session_id:
                recovery = item.get("recovery")
                if isinstance(recovery, dict):
                    recovery_status = str(recovery.get("status") or "")
                break
    payload = canonical_success(
        result_kind="rdx.capture.open",
        data={
            "context_id": context,
            "capture_file_id": capture_file_id,
            "capture_path": file_path,
            "session_id": session_id,
            "active_event_id": active_event_id,
            "recovery_status": recovery_status or "ready",
            "runtime": runtime_snapshot if isinstance(runtime_snapshot, dict) else {},
            "context": context_data,
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


async def _cmd_session_preview(args: argparse.Namespace) -> int:
    context = str(args.daemon_context)
    if args.session_preview_cmd == "on":
        call_args: Dict[str, Any] = {}
        if getattr(args, "session_id", None):
            call_args["session_id"] = str(args.session_id)
        payload = _daemon_exec("rd.session.open_preview", call_args, context=context)
        _print_json(payload)
        return EXIT_OK if bool(payload.get("ok")) else EXIT_RUNTIME_ERR
    if args.session_preview_cmd == "off":
        payload = _daemon_exec("rd.session.close_preview", {}, context=context)
        _print_json(payload)
        return EXIT_OK if bool(payload.get("ok")) else EXIT_RUNTIME_ERR
    if args.session_preview_cmd == "status":
        status_payload = _daemon_status_payload(context)
        status_data = status_payload.get("data") if isinstance(status_payload.get("data"), dict) else {}
        if not bool(status_data.get("running")):
            result = canonical_success(
                result_kind="rdx.session.preview.status",
                data={
                    "context_id": context,
                    "running": False,
                    "has_session": False,
                    "current_session_id": "",
                    "preview": {"enabled": False, "available": False},
                    "runtime": {},
                    "daemon": status_data,
                },
                transport="cli",
            )
            _print_json(result)
            return EXIT_OK
        try:
            payload = _daemon_exec("rd.session.get_context", {}, context=context)
        except Exception as exc:  # noqa: BLE001
            _print_json(
                canonical_error(
                    result_kind="rdx.session.preview.status",
                    code=str(getattr(exc, "code", "") or "preview_status_failed"),
                    category=str(getattr(exc, "category", "") or "runtime"),
                    message=f"preview status failed: {exc}",
                    details={"context_id": context, "daemon": status_data},
                    transport="cli",
                ),
            )
            return EXIT_RUNTIME_ERR
        if not bool(payload.get("ok")):
            _print_json(
                canonical_error(
                    result_kind="rdx.session.preview.status",
                    code=str((payload.get("error") or {}).get("code") or "preview_status_failed"),
                    category=str((payload.get("error") or {}).get("category") or "runtime"),
                    message=str((payload.get("error") or {}).get("message") or "preview status failed"),
                    details={"context_id": context, "source": payload},
                    transport="cli",
                ),
            )
            return EXIT_RUNTIME_ERR
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
        result = canonical_success(
            result_kind="rdx.session.preview.status",
            data={
                "context_id": str(data.get("context_id") or context),
                "current_session_id": str(data.get("current_session_id") or ""),
                "preview": dict(data.get("preview") or {}),
                "runtime": dict(runtime),
                "running": True,
                "has_session": bool(str(runtime.get("session_id") or data.get("current_session_id") or "").strip()),
            },
            transport="cli",
        )
        _print_json(result)
        return EXIT_OK
    raise RuntimeError("unsupported session preview command")


async def _cmd_diff_pipeline(args: argparse.Namespace) -> int:
    context = str(args.daemon_context)
    try:
        session_id = _default_session_id(args.session_id, context=context)
    except RuntimeError as exc:
        _print_json(_session_required_error_payload("rdx.diff.pipeline", context, str(exc)))
        return EXIT_RUNTIME_ERR
    payload = _daemon_exec(
        "rd.event.diff_pipeline_state",
        {"session_id": session_id, "event_a": int(args.event_a), "event_b": int(args.event_b)},
        context=context,
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
    context = str(args.daemon_context)
    try:
        session_id = _default_session_id(args.session_id, context=context)
    except RuntimeError as exc:
        _print_json(_session_required_error_payload("rdx.assert.pipeline", context, str(exc)))
        return EXIT_RUNTIME_ERR
    payload = _daemon_exec(
        "rd.event.diff_pipeline_state",
        {"session_id": session_id, "event_a": int(args.event_a), "event_b": int(args.event_b)},
        context=context,
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
    parser.add_argument("--version", action="version", version=f"rdx {TOOL_VERSION}")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    parser.add_argument("--daemon-context", default="default", help="Daemon state namespace (default: default)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_session_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--session-id", default=None)

    def add_format_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--format", choices=("json", "tsv"), default="json")

    def add_event_arg(command_parser: argparse.ArgumentParser, *, required: bool = False) -> None:
        command_parser.add_argument("--event-id", type=int, required=required, default=None)

    p_version = sub.add_parser("version", help="Print version and public contract metadata")
    p_version.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")

    p_doctor = sub.add_parser("doctor", help="Validate CLI runtime setup")
    p_doctor.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_tools = sub.add_parser("tools", help="Catalog discovery")
    s_tools = p_tools.add_subparsers(dest="tools_cmd", required=True)
    p_tools_list = s_tools.add_parser("list", help="List catalog-defined rd.* tools")
    p_tools_list.add_argument("--namespace", default="", help="Filter by rd.* namespace, such as capture or pipeline")
    p_tools_list.add_argument("--limit", type=int, default=0, help="Maximum tools to return; 0 means no limit")
    p_tools_list.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_tools_search = s_tools.add_parser("search", help="Search catalog-defined rd.* tools")
    p_tools_search.add_argument("query")
    p_tools_search.add_argument("--limit", type=int, default=20)
    p_tools_search.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

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
    p_context_status = s_context.add_parser("status", help="Print the current runtime context snapshot")
    p_context_status.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_context_update = s_context.add_parser("update", help="Update agent-facing context fields")
    p_context_update.add_argument("--key", required=True, choices=("notes", "focus_pixel", "focus_resource_id", "focus_shader_id"))
    p_context_update.add_argument("--value", required=True)
    p_context_update.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_context_list = s_context.add_parser("list", help="List known daemon contexts")
    p_context_list.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_context_clear = s_context.add_parser("clear")
    p_context_clear.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_call = sub.add_parser("call", help="Call any rd.* operation")
    p_call.add_argument("operation")
    p_call_args = p_call.add_mutually_exclusive_group()
    p_call_args.add_argument("--args-json", default=None)
    p_call_args.add_argument("--args-file", default=None)
    p_call.add_argument("--format", choices=("json", "tsv"), default="json")
    p_call.add_argument("--remote", action="store_true")

    p_capture = sub.add_parser("capture", help="Capture session helpers")
    s_capture = p_capture.add_subparsers(dest="capture_cmd", required=True)
    p_capture_open = s_capture.add_parser("open")
    p_capture_open.add_argument("--file", required=True)
    p_capture_open.add_argument("--frame-index", type=int, default=0)
    p_capture_open.add_argument("--artifact-dir", default=str(artifacts_dir().resolve()))
    p_capture_open.add_argument("--preview", action="store_true")
    s_capture.add_parser("status")

    p_session = sub.add_parser("session", help="Session helpers")
    s_session = p_session.add_subparsers(dest="session_cmd", required=True)
    p_session_preview = s_session.add_parser("preview", help="Human preview controls")
    s_session_preview = p_session_preview.add_subparsers(dest="session_preview_cmd", required=True)
    p_session_preview_on = s_session_preview.add_parser("on")
    p_session_preview_on.add_argument("--session-id", default=None)
    s_session_preview.add_parser("off")
    s_session_preview.add_parser("status")

    p_completion = sub.add_parser("completion", help="Generate shell completion script")
    p_completion.add_argument("shell", choices=("powershell", "bash", "zsh", "fish"))

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
    p_vfs_tree.add_argument("--max-nodes", type=int, default=2000)
    p_vfs_tree.add_argument("--format", choices=("json", "tsv"), default="json")

    p_event = sub.add_parser("event", help="Event navigation facade")
    s_event = p_event.add_subparsers(dest="event_cmd", required=True)
    p_event_list = s_event.add_parser("list", help="List event actions")
    add_session_arg(p_event_list)
    add_format_arg(p_event_list)
    p_event_show = s_event.add_parser("show", help="Show event action details")
    add_session_arg(p_event_show)
    add_format_arg(p_event_show)
    add_event_arg(p_event_show, required=True)

    p_pipeline = sub.add_parser("pipeline", help="Pipeline inspection facade")
    s_pipeline = p_pipeline.add_subparsers(dest="pipeline_cmd", required=True)
    p_pipeline_show = s_pipeline.add_parser("show", help="Show pipeline summary")
    add_session_arg(p_pipeline_show)
    add_format_arg(p_pipeline_show)
    add_event_arg(p_pipeline_show)
    p_pipeline_section = s_pipeline.add_parser("section", help="Show one shader-stage pipeline section")
    add_session_arg(p_pipeline_section)
    add_format_arg(p_pipeline_section)
    add_event_arg(p_pipeline_section)
    p_pipeline_section.add_argument("--stage", required=True)

    p_shader = sub.add_parser("shader", help="Shader inspection facade")
    s_shader = p_shader.add_subparsers(dest="shader_cmd", required=True)
    for shader_cmd, help_text in (
        ("source", "Show shader source or source fallback"),
        ("disasm", "Show shader disassembly"),
    ):
        p_shader_cmd = s_shader.add_parser(shader_cmd, help=help_text)
        add_session_arg(p_shader_cmd)
        add_format_arg(p_shader_cmd)
        add_event_arg(p_shader_cmd)
        p_shader_cmd.add_argument("--stage", required=True)
    p_shader_constants = s_shader.add_parser("constants", help="Show shader constant-buffer contents")
    add_session_arg(p_shader_constants)
    add_format_arg(p_shader_constants)
    add_event_arg(p_shader_constants)
    p_shader_constants.add_argument("--stage", required=True)
    p_shader_constants.add_argument("--slot", required=True, type=int)

    p_export = sub.add_parser("export", help="Export facade")
    s_export = p_export.add_subparsers(dest="export_cmd", required=True)
    p_export_screenshot = s_export.add_parser("screenshot", help="Export event output screenshot")
    add_session_arg(p_export_screenshot)
    add_format_arg(p_export_screenshot)
    add_event_arg(p_export_screenshot)
    p_export_screenshot.add_argument("--out", required=True)
    for export_cmd, help_text in (("texture", "Export a texture resource"), ("buffer", "Export a buffer resource")):
        p_export_cmd = s_export.add_parser(export_cmd, help=help_text)
        add_session_arg(p_export_cmd)
        add_format_arg(p_export_cmd)
        p_export_cmd.add_argument("--resource-id", required=True)
        p_export_cmd.add_argument("--out", required=True)
    p_export_mesh = s_export.add_parser("mesh", help="Export event mesh")
    add_session_arg(p_export_mesh)
    add_format_arg(p_export_mesh)
    add_event_arg(p_export_mesh)
    p_export_mesh.add_argument("--out", required=True)

    p_pixel = sub.add_parser("pixel", help="Pixel inspection facade")
    s_pixel = p_pixel.add_subparsers(dest="pixel_cmd", required=True)
    for pixel_cmd, help_text in (("value", "Read one pixel value"), ("history", "Show one pixel history")):
        p_pixel_cmd = s_pixel.add_parser(pixel_cmd, help=help_text)
        add_session_arg(p_pixel_cmd)
        add_format_arg(p_pixel_cmd)
        add_event_arg(p_pixel_cmd)
        p_pixel_cmd.add_argument("--resource-id", required=True)
        p_pixel_cmd.add_argument("--x", required=True, type=int)
        p_pixel_cmd.add_argument("--y", required=True, type=int)

    p_resource = sub.add_parser("resource", help="Resource inspection facade")
    s_resource = p_resource.add_subparsers(dest="resource_cmd", required=True)
    p_resource_list = s_resource.add_parser("list", help="List capture resources")
    add_session_arg(p_resource_list)
    add_format_arg(p_resource_list)
    for resource_cmd, help_text in (("show", "Show resource details"), ("usage", "Show resource usage")):
        p_resource_cmd = s_resource.add_parser(resource_cmd, help=help_text)
        add_session_arg(p_resource_cmd)
        add_format_arg(p_resource_cmd)
        p_resource_cmd.add_argument("--resource-id", required=True)

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
    if args.command == "version":
        return _cmd_version(args)

    if args.command == "doctor":
        return _cmd_doctor(args)

    if args.command == "tools":
        if args.tools_cmd == "list":
            return _cmd_tools_list(args)
        if args.tools_cmd == "search":
            return _cmd_tools_search(args)

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
        if args.context_cmd == "status":
            return _cmd_context_status(args)
        if args.context_cmd == "update":
            return _cmd_context_update(args)
        if args.context_cmd == "list":
            return _cmd_context_list(args)
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

    if args.command in {"event", "pipeline", "shader", "export", "pixel", "resource"}:
        return await _cmd_facade(args)

    if args.command == "capture":
        if args.capture_cmd == "open":
            return await _cmd_capture_open(args)
        if args.capture_cmd == "status":
            return _cmd_capture_status(args)

    if args.command == "session":
        if args.session_cmd == "preview":
            return await _cmd_session_preview(args)

    if args.command == "completion":
        return _cmd_completion(args)

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
        _print_json(_exception_error_payload("rdx.cli", exc, transport="cli"))
        code = EXIT_RUNTIME_ERR
    raise SystemExit(int(code))


if __name__ == "__main__":
    main()
