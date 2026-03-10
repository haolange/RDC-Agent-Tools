#!/usr/bin/env python3
"""Run dual-sample contract checks for all catalog-defined rd.* tools via MCP and daemon CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from rdx.runtime_paths import ensure_tools_root_env, ensure_runtime_dirs, artifacts_dir, binaries_root, pymodules_dir
from scripts._shared import extract_json_payload, resolve_repo_path
from rdx.timeout_policy import HARNESS_DEFAULT_TIMEOUT_S, REMOTE_CONNECT_DEFAULT_TIMEOUT_MS, harness_timeout_s

CANONICAL_KEYS = {"schema_version", "tool_version", "result_kind", "ok", "data", "artifacts", "error"}
DESTRUCTIVE_TAIL = ("rd.capture.close_replay", "rd.capture.close_file", "rd.core.shutdown")
SESSION_ERROR_SNIPPETS = (
    "Unknown session_id",
    "session_id",
    "Unknown capture_file_id",
    "capture_file_id",
    "No active session",
)
REMOTE_APP_DEPENDENCY_SNIPPETS = (
    "requires_remote_device",
    "requires_app_integration",
    "App API requires in-process RenderDoc instrumentation",
    "Remote target interaction requires a live RenderDoc remote endpoint",
)


SAMPLE_COMPATIBILITY_SNIPPETS = (
    "DebugPixel returned invalid trace",
    "invalid trace",
    "unsupported capture format",
    "unsupported version",
    "could not parse capture",
    "Failed to open capture",
)

SESSION_RETRY_LIMIT = 2


def _tools_root() -> Path:
    return ensure_tools_root_env()


def _catalog_path() -> Path:
    return _tools_root() / "spec" / "tool_catalog.json"


def _effective_timeout_s(tool_name: str, args: dict[str, Any], requested: float | None = None) -> float:
    policy_timeout = harness_timeout_s(tool_name, args)
    if requested is None or requested <= 0:
        return policy_timeout
    return max(float(requested), policy_timeout)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_data(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _payload_error(payload: dict[str, Any] | None) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    err = payload.get("error")
    if isinstance(err, dict):
        code = str(err.get("code") or "").strip().lower()
        message = str(err.get("message") or payload.get("error_message") or "")
        return code, message
    legacy_code = str(payload.get("error_code") or "").strip().lower()
    message = str(payload.get("error_message") or "")
    return legacy_code, message


def _payload_error_details(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    err = payload.get("error")
    if isinstance(err, dict):
        details = err.get("details")
        return details if isinstance(details, dict) else {}
    details = payload.get("details")
    return details if isinstance(details, dict) else {}


def _coalesce_error(
    payload: dict[str, Any] | None,
    raw: str | None,
    exc: str | None,
) -> tuple[str, str]:
    code, message = _payload_error(payload)
    if message:
        return code, message

    if isinstance(raw, str) and raw.strip():
        parsed = extract_json_payload(raw)
        if parsed is not None:
            parsed_code, parsed_message = _payload_error(parsed)
            if parsed_message:
                return parsed_code or code, parsed_message

    if isinstance(exc, str):
        text = str(exc).strip()
        if text:
            return code, text

    return code, message


def _classify_transport_impact(tool: str, matrix: str, transport: str) -> str:
    if tool.startswith("rd.remote.") or matrix == "remote":
        if matrix == "remote":
            return "only affects remote_id flow"
        return "only affects remote transport"
    if transport == "daemon":
        return "only affects daemon transport"
    if transport == "mcp":
        return "only affects MCP transport"
    return "full flow"


def _is_remote_app_dependency_error(message: str, code: str) -> bool:
    haystack = " ".join([str(code or ""), str(message or "")]).lower()
    return any(snippet.lower() in haystack for snippet in REMOTE_APP_DEPENDENCY_SNIPPETS)


def _is_sample_compatibility_error(message: str, code: str) -> bool:
    haystack = " ".join([str(code or ""), str(message or "")]).lower()
    return any(snippet in haystack for snippet in SAMPLE_COMPATIBILITY_SNIPPETS)


def _is_scope_skip_item(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "")
    if status == "scope_skip":
        return True
    return str(item.get("issue_type") or "") == "scope_skip"


def _scope_skip_classification(
    tool: str,
    payload: dict[str, Any] | None,
    message: str,
    code: str,
    transport_scope: str,
) -> tuple[str, str, str, str, str, str] | None:
    lower_msg = str(message or "").lower()
    details = _payload_error_details(payload)
    capability = str(details.get("capability") or "").strip().lower()
    optional = bool(details.get("optional", False))

    if "unknown remote_id" in lower_msg or _is_remote_app_dependency_error(message, code) or tool.startswith("rd.app."):
        return (
            "scope_skip",
            message or "remote/app dependency insufficient",
            code or "remote_app_dependency",
            "remote_app_dependency",
            "Keep app/remote dependency gaps as scope_skip in local smoke runs",
            "only affects app/remote dependency path",
        )

    if _is_sample_compatibility_error(message, code):
        return (
            "scope_skip",
            message or "sample compatibility issue",
            code or "sample_compatibility",
            "sample_compatibility",
            "Keep sample-specific replay limitations independent from issue/blocker counts",
            "only affects sample-specific replay path",
        )

    if optional and capability == "mesh_post_transform":
        return (
            "scope_skip",
            message or "post-vs/gs extraction unavailable in this build",
            code or "capability_boundary",
            "capability_boundary",
            "Treat build capability gaps as scope_skip instead of local chain failures",
            "only affects mesh post-transform extraction",
        )

    if optional and capability == "shader_binary_export":
        return (
            "scope_skip",
            message or "shader binary extraction unavailable in this replay backend",
            code or "capability_boundary",
            "capability_boundary",
            "Keep shader binary extraction backend gaps out of issue/blocker counts",
            "only affects shader binary export path",
        )

    if optional and capability == "shader_compile":
        return (
            "scope_skip",
            message or "on-host shader compilation unavailable",
            code or "capability_boundary",
            "capability_boundary",
            "Keep host shader compiler availability as scope_skip for local smoke",
            "only affects shader compilation path",
        )

    if tool == "rd.perf.describe_counter" and "counter not found" in lower_msg:
        return (
            "scope_skip",
            message or "counter unavailable for this sample",
            code or "capability_boundary",
            "capability_boundary",
            "Use an enumerated counter_id when available; otherwise keep the perf detail path as scope_skip",
            "only affects perf counter detail path",
        )

    if tool in {"rd.mesh.get_post_vs_data", "rd.mesh.get_post_gs_data"} and "post-vs/gs extraction is not available" in lower_msg:
        return (
            "scope_skip",
            message or "post-vs/gs extraction unavailable in this build",
            code or "capability_boundary",
            "capability_boundary",
            "Treat build capability gaps as scope_skip instead of local chain failures",
            "only affects mesh post-transform extraction",
        )

    if tool in {"rd.shader.extract_binary", "rd.shader.save_binary"} and "shader binary extraction is not available" in lower_msg:
        return (
            "scope_skip",
            message or "shader binary extraction unavailable in this replay backend",
            code or "capability_boundary",
            "capability_boundary",
            "Keep shader binary extraction backend gaps out of issue/blocker counts",
            "only affects shader binary export path",
        )

    if tool == "rd.shader.compile" and "on-host shader compilation is not configured" in lower_msg:
        return (
            "scope_skip",
            message or "on-host shader compilation unavailable",
            code or "capability_boundary",
            "capability_boundary",
            "Keep host shader compiler availability as scope_skip for local smoke",
            "only affects shader compilation path",
        )

    if tool in {"rd.shader.get_debug_state", "rd.debug.step", "rd.debug.continue", "rd.debug.get_variables"} and (
        "unknown shader_debug_id" in lower_msg or "no shader_debug_id" in lower_msg
    ):
        return (
            "scope_skip",
            message or "shader debug session unavailable",
            code or "sample_compatibility",
            "sample_compatibility",
            "Keep dependent shader debug tools as scope_skip when a debug trace cannot be created",
            "only affects shader debug chain",
        )

    return None


def _make_scope_skip_item(
    *,
    tool: str,
    transport: str,
    matrix: str,
    reason: str,
    issue_type: str,
    fix_hint: str,
    impact_scope: str,
    args: dict[str, Any],
    error_code: str,
    evidence: str,
    repro_command: str,
    contract: bool,
    sample_compatibility: bool | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "tool": tool,
        "transport": transport,
        "matrix": matrix,
        "status": "scope_skip",
        "reason": reason,
        "issue_type": issue_type,
        "fix_hint": fix_hint,
        "impact_scope": impact_scope,
        "ok": False,
        "callable": False,
        "contract": contract,
        "args": args,
        "error_code": error_code,
        "evidence": evidence,
        "repro_command": repro_command,
    }
    if sample_compatibility is not None:
        item["sample_compatibility"] = sample_compatibility
    return item


def _make_covered_pass_item(
    *,
    tool: str,
    transport: str,
    matrix: str,
    covered_by_tool: str,
    reason: str,
    issue_type: str,
    fix_hint: str,
    impact_scope: str,
    args: dict[str, Any],
    error_code: str,
    evidence: str,
    repro_command: str,
) -> dict[str, Any]:
    return {
        "tool": tool,
        "transport": transport,
        "matrix": matrix,
        "status": "pass",
        "reason": reason,
        "issue_type": issue_type,
        "fix_hint": fix_hint,
        "impact_scope": impact_scope,
        "ok": False,
        "callable": False,
        "contract": False,
        "args": args,
        "error_code": error_code,
        "evidence": evidence,
        "repro_command": repro_command,
        "covered_by_tool": covered_by_tool,
        "covered_by_status": "scope_skip",
        "covered_scope_skip": True,
    }


def _is_session_related_failure(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if bool(payload.get("ok")):
        return False

    code, message = _payload_error(payload)
    haystack = " ".join([str(code or ""), str(message or "")]).lower()
    if not haystack:
        return False
    return any(snippet.lower() in haystack for snippet in SESSION_ERROR_SNIPPETS)


def _is_remote_matrix_tool(tool_name: str, param_names: list[str]) -> bool:
    if tool_name.startswith("rd.remote."):
        return True
    return "remote_id" in param_names


def _server_env() -> dict[str, str]:
    root = _tools_root()
    artifacts = Path(os.environ.get("RDX_ARTIFACT_DIR", str(artifacts_dir())))
    renderdoc_dir = Path(os.environ.get("RDX_RENDERDOC_PATH", str(pymodules_dir())))
    env = dict(os.environ)
    env["RDX_TOOLS_ROOT"] = str(root)
    env.setdefault("RDX_LOG_LEVEL", "ERROR")
    env["RDX_ARTIFACT_DIR"] = str(artifacts)
    env["RDX_RENDERDOC_PATH"] = str(renderdoc_dir)
    return env


def _prepare_artifacts(root: Path) -> dict[str, Path]:
    artifacts = Path(os.environ.get("RDX_ARTIFACT_DIR", str(root / "intermediate" / "artifacts")))
    artifacts.mkdir(parents=True, exist_ok=True)

    sample_file = artifacts / "sample.bin"
    sample_file.write_bytes(b"\x00\x01\x02\x03")

    png_a = artifacts / "a.png"
    png_b = artifacts / "b.png"
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\x0cIDAT\x08\x99c```\xf8\xff\x1f\x00\x03\x03\x01\x00\xb4\x89\xc5\x0f"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    png_a.write_bytes(png_bytes)
    png_b.write_bytes(png_bytes)

    text_a = artifacts / "text_a.txt"
    text_b = artifacts / "text_b.txt"
    text_a.write_text("left\n", encoding="utf-8")
    text_b.write_text("right\n", encoding="utf-8")

    return {
        "artifacts": artifacts,
        "sample": sample_file,
        "png_a": png_a,
        "png_b": png_b,
        "text_a": text_a,
        "text_b": text_b,
        "zip_out": artifacts / "pack_output.zip",
        "capture_copy": artifacts / "capture_copy.rdc",
    }


@dataclass
class SampleState:
    matrix: str
    rdc_path: Path
    session_id: str | None = None
    capture_file_id: str | None = None
    event_id: int = 0
    texture_id: str | None = None
    resource_id: str | None = None
    buffer_id: str | None = None
    shader_id: str | None = None
    shader_debug_id: str | None = None
    shader_debug_error_code: str = ""
    shader_debug_issue_type: str = ""
    shader_debug_reason: str = ""
    debug_x: int = 1
    debug_y: int = 1
    debug_sample: int | None = None
    debug_view: int | None = None
    debug_primitive: int | None = None
    debug_pc: int = 0
    debug_target: dict[str, Any] = field(default_factory=dict)
    debug_params: dict[str, Any] = field(default_factory=dict)
    remote_id: str | None = None
    remote_error_code: str = ""
    remote_issue_type: str = "remote_endpoint"
    remote_reason: str = ""
    counter_id: int | None = None
    counter_error_code: str = ""
    counter_issue_type: str = ""
    counter_reason: str = ""
    sample_compatibility: bool = True
    known_session_ids: list[str] = field(default_factory=list)
    known_capture_file_ids: list[str] = field(default_factory=list)


class DaemonExecutor:
    def __init__(self, root: Path, context_name: str) -> None:
        self.root = root
        self.context_name = context_name
        self.env = _server_env()
        self.started = False

    def _run_cli(self, args: list[str], *, timeout_s: float = 30.0) -> tuple[int, str, str]:
        cmd = [sys.executable, "cli/run_cli.py", "--daemon-context", self.context_name, *args]
        proc = subprocess.run(
            cmd,
            cwd=str(self.root),
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout_s)),
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    async def startup(self) -> tuple[bool, str]:
        def _start() -> tuple[bool, str]:
            code, out, err = self._run_cli(["daemon", "start"], timeout_s=40.0)
            payload = extract_json_payload(out)
            if code == 0 and payload and bool(payload.get("ok")):
                return True, ""
            detail = (out + "\n" + err).strip()
            return False, detail[:2000]

        ok, detail = await asyncio.to_thread(_start)
        self.started = ok
        return ok, detail

    async def shutdown(self) -> tuple[bool, str]:
        def _stop() -> tuple[bool, str]:
            code, out, err = self._run_cli(["daemon", "stop"], timeout_s=20.0)
            payload = extract_json_payload(out)
            if code == 0 and payload and bool(payload.get("ok")):
                return True, ""
            detail = (out + "\n" + err).strip()
            return False, detail[:2000]

        return await asyncio.to_thread(_stop)

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> tuple[dict[str, Any] | None, str, str]:
        def _call() -> tuple[dict[str, Any] | None, str, str]:
            try:
                effective_timeout_s = _effective_timeout_s(name, args, timeout_s)
                code, out, err = self._run_cli(
                    [
                        "call",
                        name,
                        "--args-json",
                        json.dumps(args, ensure_ascii=False),
                        "--json",
                        "--connect",
                    ],
                    timeout_s=effective_timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                return None, "", f"call timeout: {exc}"

            payload = extract_json_payload(out)
            if payload is None:
                detail = (err or out).strip()
                return None, out, f"non-json daemon call output (exit={code}): {detail[:500]}"
            return payload, out, err.strip()

        return await asyncio.to_thread(_call)


def _remote_connect_args() -> dict[str, Any]:
    args: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 38920,
        "timeout_ms": REMOTE_CONNECT_DEFAULT_TIMEOUT_MS,
    }
    transport = str(os.environ.get("RDX_REMOTE_CONNECT_TRANSPORT", "renderdoc") or "renderdoc").strip().lower()
    options: dict[str, Any] = {}
    if transport and transport != "renderdoc":
        options["transport"] = transport
    if serial := str(os.environ.get("RDX_REMOTE_DEVICE_SERIAL", "") or "").strip():
        options["device_serial"] = serial
    if local_port := str(os.environ.get("RDX_REMOTE_LOCAL_PORT", "") or "").strip():
        try:
            options["local_port"] = int(local_port)
        except ValueError:
            pass
    for env_name, option_name in (
        ("RDX_REMOTE_INSTALL_APK", "install_apk"),
        ("RDX_REMOTE_PUSH_CONFIG", "push_config"),
    ):
        raw = str(os.environ.get(env_name, "") or "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            options[option_name] = True
        elif raw in {"0", "false", "no", "off"}:
            options[option_name] = False
    if options:
        args["options"] = options
    return args

async def _ensure_context(
    call_fn: Any,
    state: SampleState,
    files: dict[str, Path],
    *,
    need_capture: bool = True,
    need_remote: bool = True,
) -> None:
    if need_capture and state.session_id and state.capture_file_id and (not need_remote or state.remote_id):
        return
    if not need_capture and need_remote and state.remote_id:
        return

    if need_capture and not state.sample_compatibility:
        return

    state.sample_compatibility = True

    await call_fn(
        "rd.core.init",
        {
            "global_env": {"artifact_dir": str(files["artifacts"])},
            "enable_remote": True,
            "enable_app_api": True,
        },
        timeout_s=25.0,
    )

    if need_capture:
        if not state.rdc_path.is_file():
            return
        payload, _, _ = await call_fn(
            "rd.capture.open_file",
            {"file_path": str(state.rdc_path), "read_only": True},
            timeout_s=30.0,
        )
        if payload and payload.get("ok"):
            data = _payload_data(payload)
            state.capture_file_id = str(payload.get("capture_file_id") or data.get("capture_file_id") or "")
            if not state.capture_file_id:
                state.capture_file_id = None
            else:
                _remember_capture_handle(state, state.capture_file_id)
        elif payload:
            code, message = _payload_error(payload)
            if _is_sample_compatibility_error(message, code):
                state.sample_compatibility = False
                return
            if _is_remote_app_dependency_error(message, code):
                return
            state.capture_file_id = None

    if need_capture and state.capture_file_id:
        payload, _, _ = await call_fn(
            "rd.capture.open_replay",
            {"capture_file_id": state.capture_file_id, "options": {}},
            timeout_s=35.0,
        )
        if payload and payload.get("ok"):
            data = _payload_data(payload)
            state.session_id = str(payload.get("session_id") or data.get("session_id") or "")
            if not state.session_id:
                state.session_id = None
            else:
                _remember_session_handle(state, state.session_id)
        elif payload:
            code, message = _payload_error(payload)
            if _is_sample_compatibility_error(message, code):
                state.sample_compatibility = False
                return
            if _is_remote_app_dependency_error(message, code):
                return
            state.session_id = None

    if need_capture and state.session_id:
        state.counter_id = None
        state.counter_error_code = ""
        state.counter_issue_type = ""
        state.counter_reason = ""
        state.event_id = 0
        state.shader_debug_id = None
        state.shader_debug_error_code = ""
        state.shader_debug_issue_type = ""
        state.shader_debug_reason = ""
        state.debug_x = 1
        state.debug_y = 1
        state.debug_sample = None
        state.debug_view = None
        state.debug_primitive = None
        state.debug_pc = 0
        state.debug_target = {}
        state.debug_params = {}

        await call_fn("rd.replay.set_frame", {"session_id": state.session_id, "frame_index": 0}, timeout_s=20.0)

        draw_candidates: list[dict[str, Any]] = []
        actions_payload, _, _ = await call_fn(
            "rd.event.get_actions",
            {"session_id": state.session_id, "include_markers": True, "include_drawcalls": True},
            timeout_s=20.0,
        )
        if actions_payload and actions_payload.get("ok"):
            actions = _payload_data(actions_payload).get("actions", [])
            if isinstance(actions, list):
                fallback_event = None
                first_draw_event = None
                for item in _walk_action_nodes(actions):
                    raw_event_id = item.get("event_id")
                    try:
                        event_id = int(raw_event_id)
                    except Exception:
                        continue
                    if event_id <= 0:
                        continue
                    if fallback_event is None:
                        fallback_event = event_id
                    flags = item.get("flags")
                    raw_outputs = item.get("outputs")
                    outputs = [
                        str(value)
                        for value in raw_outputs
                        if str(value) and str(value) != "ResourceId::0"
                    ] if isinstance(raw_outputs, list) else []
                    if isinstance(flags, dict) and bool(flags.get("is_draw")):
                        if first_draw_event is None:
                            first_draw_event = event_id
                        if outputs:
                            draw_candidates.append({"event_id": event_id, "outputs": outputs})
                            if state.event_id <= 0 or state.event_id == first_draw_event:
                                state.event_id = event_id
                        elif state.event_id <= 0:
                            state.event_id = event_id
                if fallback_event is not None and state.event_id <= 0:
                    state.event_id = fallback_event

        if state.event_id > 0:
            await call_fn(
                "rd.event.set_active",
                {"session_id": state.session_id, "event_id": int(state.event_id)},
                timeout_s=20.0,
            )

        tx_payload, _, _ = await call_fn(
            "rd.resource.list_textures",
            {"session_id": state.session_id},
            timeout_s=20.0,
        )
        if tx_payload and tx_payload.get("ok"):
            textures = _payload_data(tx_payload).get("textures", [])
            if isinstance(textures, list) and textures and isinstance(textures[0], dict):
                t0 = textures[0]
                state.texture_id = str(t0.get("texture_id") or t0.get("resource_id") or "")
                state.resource_id = state.texture_id or state.resource_id
        if draw_candidates:
            preferred_texture = str(draw_candidates[0]["outputs"][0])
            if preferred_texture:
                state.texture_id = preferred_texture
                state.resource_id = preferred_texture

        bu_payload, _, _ = await call_fn(
            "rd.resource.list_buffers",
            {"session_id": state.session_id},
            timeout_s=20.0,
        )
        if bu_payload and bu_payload.get("ok"):
            buffers = _payload_data(bu_payload).get("buffers", [])
            if isinstance(buffers, list) and buffers and isinstance(buffers[0], dict):
                b0 = buffers[0]
                state.buffer_id = str(b0.get("buffer_id") or b0.get("resource_id") or "")

        sh_payload, _, _ = await call_fn(
            "rd.pipeline.get_shader",
            {"session_id": state.session_id, "stage": "ps"},
            timeout_s=20.0,
        )
        if sh_payload and sh_payload.get("ok"):
            shader = _payload_data(sh_payload).get("shader", {})
            if isinstance(shader, dict):
                state.shader_id = str(shader.get("shader_id") or "")
        elif sh_payload:
            code, message = _payload_error(sh_payload)
            state.shader_debug_error_code = code or "shader_unavailable"
            state.shader_debug_issue_type = "capability_boundary"
            state.shader_debug_reason = message or "shader metadata unavailable"

        counter_payload, _, _ = await call_fn(
            "rd.perf.enumerate_counters",
            {"session_id": state.session_id},
            timeout_s=20.0,
        )
        if counter_payload and counter_payload.get("ok"):
            counters = _payload_data(counter_payload).get("counters", [])
            if isinstance(counters, list):
                for counter in counters:
                    if not isinstance(counter, dict):
                        continue
                    try:
                        state.counter_id = int(counter.get("counter_id"))
                    except Exception:
                        continue
                    if state.counter_id > 0:
                        break
                if state.counter_id is None:
                    state.counter_error_code = "counter_unavailable"
                    state.counter_issue_type = "capability_boundary"
                    state.counter_reason = "no usable counter_id returned by enumerate_counters"
        elif counter_payload:
            code, message = _payload_error(counter_payload)
            state.counter_error_code = code or "counter_unavailable"
            state.counter_issue_type = "capability_boundary"
            state.counter_reason = message or "performance counters unavailable"

        if state.shader_id:
            debug_candidate, last_debug_code, last_debug_message = await _pick_shader_debug_candidate(
                call_fn,
                state,
                draw_candidates,
            )
            debug_payload = None
            if debug_candidate is not None:
                state.event_id = int(debug_candidate["event_id"])
                state.texture_id = str(debug_candidate["texture_id"])
                state.resource_id = state.texture_id or state.resource_id
                state.debug_params = dict(debug_candidate["params"])
                state.debug_target = dict(state.debug_params.get("target") or {})
                state.debug_x = int(state.debug_params.get("x", 1))
                state.debug_y = int(state.debug_params.get("y", 1))
                state.debug_sample = state.debug_params.get("sample")
                state.debug_view = state.debug_params.get("view")
                state.debug_primitive = state.debug_params.get("primitive")
                debug_payload = debug_candidate["payload"]
            else:
                fallback_params: dict[str, Any] = {"x": int(state.debug_x), "y": int(state.debug_y)}
                if state.texture_id:
                    fallback_params["target"] = {"texture_id": state.texture_id}
                state.debug_params = dict(fallback_params)
                state.debug_target = dict(fallback_params.get("target") or {})
                debug_payload, _, _ = await call_fn(
                    "rd.shader.debug_start",
                    {
                        "session_id": state.session_id,
                        "mode": "pixel",
                        "event_id": int(state.event_id or 1),
                        "params": fallback_params,
                        "timeout_ms": 0,
                    },
                    timeout_s=25.0,
                )

            if debug_payload and debug_payload.get("ok"):
                data = _payload_data(debug_payload)
                state.shader_debug_id = str(debug_payload.get("shader_debug_id") or data.get("shader_debug_id") or "")
                if not state.shader_debug_id:
                    state.shader_debug_id = None
                initial_state = data.get("initial_state", {})
                if isinstance(initial_state, dict):
                    try:
                        state.debug_pc = int(initial_state.get("pc") or 0)
                    except Exception:
                        state.debug_pc = 0
                resolved_context = data.get("resolved_context", {})
                if isinstance(resolved_context, dict):
                    try:
                        state.event_id = int(resolved_context.get("event_id") or state.event_id or 0)
                    except Exception:
                        pass
                    target_ctx = resolved_context.get("target")
                    if isinstance(target_ctx, dict):
                        state.debug_target = dict(target_ctx)
                    for key, attr in (("x", "debug_x"), ("y", "debug_y"), ("sample", "debug_sample"), ("view", "debug_view"), ("primitive", "debug_primitive")):
                        if key in resolved_context:
                            setattr(state, attr, resolved_context.get(key))
                if state.session_id:
                    shader_refresh_payload, _, _ = await call_fn(
                        "rd.pipeline.get_shader",
                        {"session_id": state.session_id, "stage": "ps"},
                        timeout_s=20.0,
                    )
                    if shader_refresh_payload and shader_refresh_payload.get("ok"):
                        shader = _payload_data(shader_refresh_payload).get("shader", {})
                        if isinstance(shader, dict):
                            refreshed_shader_id = str(shader.get("shader_id") or "")
                            if refreshed_shader_id:
                                state.shader_id = refreshed_shader_id
            elif debug_payload:
                code, message = _payload_error(debug_payload)
                state.shader_debug_error_code = code or "shader_debug_unavailable"
                state.shader_debug_issue_type = (
                    "sample_compatibility" if _is_sample_compatibility_error(message, code) else "capability_boundary"
                )
                state.shader_debug_reason = message or "shader debug trace unavailable"
            else:
                state.shader_debug_error_code = last_debug_code or "shader_debug_unavailable"
                state.shader_debug_issue_type = (
                    "sample_compatibility"
                    if _is_sample_compatibility_error(last_debug_message, last_debug_code)
                    else "capability_boundary"
                )
                state.shader_debug_reason = last_debug_message or "shader debug trace unavailable"
        elif not state.shader_debug_reason:
            state.shader_debug_error_code = "shader_unavailable"
            state.shader_debug_issue_type = "capability_boundary"
            state.shader_debug_reason = "pixel shader unavailable from current pipeline state"

    if need_remote and not state.remote_id:
        state.remote_error_code = ""
        state.remote_reason = ""
        state.remote_issue_type = "remote_endpoint"
        remote_payload, remote_raw, remote_exc = await call_fn(
            "rd.remote.connect",
            _remote_connect_args(),
            timeout_s=20.0,
        )
        if remote_payload and remote_payload.get("ok"):
            data = _payload_data(remote_payload)
            state.remote_id = str(remote_payload.get("remote_id") or data.get("remote_id") or "")
            if not state.remote_id:
                state.remote_id = None
                state.remote_error_code = "remote_id_missing"
                state.remote_reason = "rd.remote.connect succeeded without returning remote_id"
            else:
                ping_payload, ping_raw, ping_exc = await call_fn(
                    "rd.remote.ping",
                    {"remote_id": state.remote_id},
                    timeout_s=10.0,
                )
                if not (ping_payload and ping_payload.get("ok")):
                    state.remote_error_code, state.remote_reason = _coalesce_error(ping_payload, ping_raw, ping_exc)
                    state.remote_error_code = state.remote_error_code or "remote_ping_failed"
                    state.remote_reason = state.remote_reason or "rd.remote.ping failed"
                    state.remote_id = None
        else:
            state.remote_error_code, state.remote_reason = _coalesce_error(remote_payload, remote_raw, remote_exc)
            state.remote_error_code = state.remote_error_code or "remote_connect_failed"
            state.remote_reason = state.remote_reason or "rd.remote.connect failed"

def _default_for_id(param: str, state: SampleState) -> Any:
    if param == "counter_id":
        return state.counter_id
    if param == "shader_debug_id":
        return state.shader_debug_id
    known = {
        "session_id": state.session_id,
        "capture_file_id": state.capture_file_id,
        "resource_id": state.resource_id or state.texture_id,
        "texture_id": state.texture_id,
        "buffer_id": state.buffer_id,
        "vertex_buffer_id": state.buffer_id,
        "index_buffer_id": state.buffer_id,
        "shader_id": state.shader_id,
        "shader_debug_id": state.shader_debug_id,
        "remote_id": state.remote_id,
        "target_id": "target_dummy",
        "capture_id": "capture_dummy",
    }
    value = known.get(param)
    if value is not None:
        return value
    if param.endswith("_id"):
        return f"{param}_dummy"
    return None


def _remember_unique(items: list[str], value: str | None) -> None:
    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)


def _remember_capture_handle(state: SampleState, capture_file_id: str | None) -> None:
    _remember_unique(state.known_capture_file_ids, capture_file_id)


def _remember_session_handle(state: SampleState, session_id: str | None) -> None:
    _remember_unique(state.known_session_ids, session_id)


def _forget_unique(items: list[str], value: str | None) -> None:
    text = str(value or "").strip()
    if not text:
        return
    while text in items:
        items.remove(text)


def _forget_capture_handle(state: SampleState, capture_file_id: str | None) -> None:
    _forget_unique(state.known_capture_file_ids, capture_file_id)


def _forget_session_handle(state: SampleState, session_id: str | None) -> None:
    _forget_unique(state.known_session_ids, session_id)


def _track_tool_side_effects(
    tool: str,
    args: dict[str, Any],
    payload: dict[str, Any] | None,
    state: SampleState,
) -> None:
    if not isinstance(payload, dict) or not bool(payload.get("ok")):
        return

    data = _payload_data(payload)
    if tool == "rd.capture.open_file":
        capture_file_id = str(payload.get("capture_file_id") or data.get("capture_file_id") or "").strip()
        if capture_file_id:
            _remember_capture_handle(state, capture_file_id)
        return

    if tool == "rd.capture.open_replay":
        session_id = str(payload.get("session_id") or data.get("session_id") or "").strip()
        if session_id:
            _remember_session_handle(state, session_id)
        return

    if tool == "rd.capture.close_replay":
        _forget_session_handle(state, str(args.get("session_id") or ""))
        return

    if tool == "rd.capture.close_file":
        _forget_capture_handle(state, str(args.get("capture_file_id") or ""))
        return

    if tool == "rd.core.shutdown":
        state.known_session_ids.clear()
        state.known_capture_file_ids.clear()


async def _cleanup_known_sessions(call_fn: Any, state: SampleState) -> None:
    for session_id in list(state.known_session_ids):
        payload, _, _ = await call_fn(
            "rd.capture.close_replay",
            {"session_id": session_id},
            timeout_s=25.0,
        )
        _, message = _payload_error(payload)
        if (payload and payload.get("ok")) or "unknown session_id" in str(message or "").lower():
            _forget_session_handle(state, session_id)
            if state.session_id == session_id:
                state.session_id = None


async def _cleanup_known_capture_handles(call_fn: Any, state: SampleState) -> None:
    for capture_file_id in list(state.known_capture_file_ids):
        payload, _, _ = await call_fn(
            "rd.capture.close_file",
            {"capture_file_id": capture_file_id},
            timeout_s=25.0,
        )
        _, message = _payload_error(payload)
        if (payload and payload.get("ok")) or "unknown capture_file_id" in str(message or "").lower():
            _forget_capture_handle(state, capture_file_id)
            if state.capture_file_id == capture_file_id:
                state.capture_file_id = None


async def _validate_success_follow_up(
    call_fn: Any,
    tool: str,
    args: dict[str, Any],
) -> tuple[str, str, str, str, str, str] | None:
    if tool != "rd.session.update_context":
        return None
    if str(args.get("key") or "").strip() != "notes":
        return None

    payload, raw, exc = await call_fn("rd.session.get_context", {}, timeout_s=15.0)
    if not isinstance(payload, dict) or not bool(payload.get("ok")):
        code, message = _coalesce_error(payload, raw, exc)
        return (
            "blocker",
            message or "rd.session.get_context follow-up failed after context update",
            code or "context_follow_up_failed",
            "main_chain",
            "Verify context update round-trip by reading rd.session.get_context after write operations",
            "context snapshot",
        )

    notes = str(_payload_data(payload).get("notes") or "")
    expected = str(args.get("value") or "")
    if notes != expected:
        return (
            "blocker",
            f"rd.session.update_context readback mismatch: expected notes={expected!r}, got {notes!r}",
            "context_follow_up_mismatch",
            "main_chain",
            "Verify context update round-trip by reading rd.session.get_context after write operations",
            "context snapshot",
        )

    return None


def _is_shutdown_transport_teardown(exc: str | None) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    return any(
        snippet in text
        for snippet in (
            "connection closed",
            "broken pipe",
            "winerror 2",
            "system cannot find the file specified",
        )
    )


async def _stabilize_destructive_tail_result(
    call_fn: Any,
    tool: str,
    args: dict[str, Any],
    payload: dict[str, Any] | None,
    raw: str,
    exc: str,
) -> tuple[dict[str, Any] | None, str, str]:
    error_code, error_message = _coalesce_error(payload, raw, exc)

    if tool == "rd.core.shutdown" and _is_shutdown_transport_teardown(error_message or exc):
        return (
            {
                "schema_version": "2.0.0",
                "tool_version": "1.0.0",
                "result_kind": "rd.core.shutdown",
                "ok": True,
                "data": {"released": {"note": "transport closed after shutdown"}},
                "artifacts": [],
                "error": None,
            },
            raw,
            "",
        )

    if tool == "rd.capture.close_file":
        capture_file_id = str(args.get("capture_file_id") or "").strip()
        if capture_file_id:
            if _is_shutdown_transport_teardown(error_message or exc):
                return (
                    {
                        "schema_version": "2.0.0",
                        "tool_version": "1.0.0",
                        "result_kind": "rd.capture.close_file",
                        "ok": True,
                        "data": {"capture_file_id": capture_file_id},
                        "artifacts": [],
                        "error": None,
                    },
                    raw,
                    "",
                )
            probe_payload, probe_raw, probe_exc = await call_fn(
                "rd.capture.get_info",
                {"capture_file_id": capture_file_id},
                timeout_s=15.0,
            )
            probe_code, probe_message = _coalesce_error(probe_payload, probe_raw, probe_exc)
            if "unknown capture_file_id" in str(probe_message or "").lower():
                return (
                    {
                        "schema_version": "2.0.0",
                        "tool_version": "1.0.0",
                        "result_kind": "rd.capture.close_file",
                        "ok": True,
                        "data": {"capture_file_id": capture_file_id},
                        "artifacts": [],
                        "error": None,
                    },
                    raw,
                    "",
                )

    return payload, raw, exc


def _ensure_fixture_inputs(files: dict[str, Path]) -> None:
    fixtures: list[tuple[Path, bytes]] = [
        (files["sample"], b"\x00\x01\x02\x03"),
        (files["text_a"], b"left\n"),
        (files["text_b"], b"right\n"),
        (files["png_a"],
         b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0cIDAT\x08\x99c```\x08\xff\x1f\x00\x03\x03\x01\x00\xb4\x89\xc5\x0f\x00\x00\x00\x00IEND\xaeB`\x82"),
        (files["png_b"],
         b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0cIDAT\x08\x99c```\x08\xff\x1f\x00\x03\x03\x01\x00\xb4\x89\xc5\x0f\x00\x00\x00\x00IEND\xaeB`\x82"),
    ]

    for path, payload in fixtures:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(payload)


def _walk_action_nodes(nodes: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        out.append(node)
        children = node.get("children")
        if isinstance(children, list):
            out.extend(_walk_action_nodes(children))
    return out


def _sample_positions(extent: int) -> list[int]:
    extent = max(int(extent), 1)
    if extent <= 2:
        return sorted({0, max(0, extent - 1)})
    return sorted({0, max(0, extent // 2), max(0, extent - 1)})


def _valid_primitive_id(value: Any) -> int | None:
    try:
        primitive = int(value)
    except Exception:
        return None
    if primitive < 0 or primitive == 0xFFFFFFFF:
        return None
    return primitive


async def _pick_shader_debug_candidate(
    call_fn: Any,
    state: SampleState,
    draw_candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, str]:
    last_code = ""
    last_message = ""

    for candidate in draw_candidates[:18]:
        try:
            event_id = int(candidate.get("event_id") or 0)
        except Exception:
            continue
        if event_id <= 0:
            continue
        outputs = [
            str(value)
            for value in candidate.get("outputs", [])
            if str(value) and str(value) != "ResourceId::0"
        ]
        if not outputs:
            continue

        for texture_id in outputs[:2]:
            details_payload, _, _ = await call_fn(
                "rd.resource.get_details",
                {"session_id": state.session_id, "resource_id": texture_id},
                timeout_s=20.0,
            )
            if not details_payload or not details_payload.get("ok"):
                continue
            details = _payload_data(details_payload).get("details", {})
            if not isinstance(details, dict):
                continue
            width = int(details.get("width") or 0)
            height = int(details.get("height") or 0)
            if width <= 0 or height <= 0:
                continue

            for x in _sample_positions(width):
                for y in _sample_positions(height):
                    history_payload, _, _ = await call_fn(
                        "rd.debug.pixel_history",
                        {
                            "session_id": state.session_id,
                            "x": int(x),
                            "y": int(y),
                            "target": {"texture_id": texture_id},
                        },
                        timeout_s=20.0,
                    )
                    if not history_payload or not history_payload.get("ok"):
                        continue
                    history = _payload_data(history_payload).get("history", [])
                    if not isinstance(history, list):
                        continue

                    matches: list[dict[str, Any]] = []
                    for item in history:
                        if not isinstance(item, dict):
                            continue
                        try:
                            hist_event = int(item.get("event_id") or 0)
                        except Exception:
                            continue
                        if hist_event != event_id:
                            continue
                        if item.get("passed") is False:
                            continue
                        if bool(item.get("shader_discarded")) or bool(item.get("unbound_ps")):
                            continue
                        matches.append(item)

                    if not matches:
                        continue

                    params: dict[str, Any] = {
                        "x": int(x),
                        "y": int(y),
                        "sample": 0,
                        "view": 0,
                        "target": {"texture_id": texture_id},
                    }
                    primitive = None
                    for item in matches:
                        primitive = _valid_primitive_id(item.get("primitive_id"))
                        if primitive is not None:
                            params["primitive"] = primitive
                            break

                    debug_payload, _, _ = await call_fn(
                        "rd.shader.debug_start",
                        {
                            "session_id": state.session_id,
                            "mode": "pixel",
                            "event_id": event_id,
                            "params": params,
                            "timeout_ms": 0,
                        },
                        timeout_s=25.0,
                    )
                    if debug_payload and debug_payload.get("ok"):
                        return (
                            {
                                "event_id": event_id,
                                "texture_id": texture_id,
                                "params": params,
                                "payload": debug_payload,
                            },
                            last_code,
                            last_message,
                        )
                    if debug_payload:
                        last_code, last_message = _payload_error(debug_payload)

    return None, last_code, last_message


def _preflight_scope_skip(
    tool: str,
    param_names: list[str],
    state: SampleState,
) -> tuple[str, str, str, str, str] | None:
    if tool == "rd.perf.describe_counter" and state.counter_id is None:
        return (
            state.counter_reason or "performance counter unavailable",
            state.counter_error_code or "counter_unavailable",
            state.counter_issue_type or "capability_boundary",
            "Use an enumerated counter_id when available; otherwise keep perf describe as scope_skip",
            "only affects perf counter detail path",
        )

    return None


def _covered_preflight_pass(
    tool: str,
    param_names: list[str],
    state: SampleState,
) -> tuple[str, str, str, str, str, str] | None:
    if "shader_debug_id" not in param_names or state.shader_debug_id:
        return None
    return (
        "rd.shader.debug_start",
        (
            "Covered by upstream rd.shader.debug_start scope_skip: "
            + (state.shader_debug_reason or "shader debug session unavailable")
        ),
        state.shader_debug_error_code or "shader_debug_unavailable",
        state.shader_debug_issue_type or "sample_compatibility",
        "Count shader debug sample limits once at rd.shader.debug_start; keep dependent tools as covered passes",
        "only affects shader debug chain",
    )


def _build_args(tool: str, param_names: list[str], state: SampleState, files: dict[str, Path]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for param in param_names:
        if hasattr(state, param):
            direct = getattr(state, param)
            if direct is not None:
                args[param] = direct
                continue

        value = _default_for_id(param, state)
        if value is not None:
            args[param] = value
            continue

        if param == "file_path":
            args[param] = str(state.rdc_path)
        elif param in {"rdc_path", "local_path"}:
            args[param] = str(state.rdc_path)
        elif param == "output_dir":
            args[param] = str(files["artifacts"])
        elif param == "output_path":
            if tool == "rd.util.pack_zip":
                args[param] = str(files["zip_out"])
            elif tool == "rd.util.diff_images":
                args[param] = str(files["artifacts"] / f"{state.matrix}_image_diff.png")
            elif tool == "rd.texture.save_to_file":
                args[param] = str(files["artifacts"] / f"{state.matrix}_texture_out.png")
            else:
                args[param] = str(files["artifacts"] / f"{state.matrix}_{tool.replace('.', '_')}.out")
        elif param == "paths":
            args[param] = [str(files["sample"]), str(files["png_a"])]
        elif param == "path" and tool.startswith("rd.vfs."):
            if tool == "rd.vfs.ls":
                args[param] = "/"
            elif tool == "rd.vfs.tree":
                args[param] = "/draws"
            elif tool == "rd.vfs.resolve":
                args[param] = "/pipeline"
            else:
                args[param] = "/context"
        elif param == "path":
            args[param] = str(files["sample"])
        elif param in {"image_a_path", "image_b_path"}:
            args[param] = str(files["png_a"] if param == "image_a_path" else files["png_b"])
        elif param == "a":
            args[param] = str(files["text_a"])
        elif param == "b":
            args[param] = str(files["text_b"])
        elif param in {"a_is_path", "b_is_path"}:
            args[param] = True
        elif param == "algo":
            args[param] = "sha256"
        elif param == "frame_index":
            args[param] = 0
        elif param in {"event_id", "event_a", "event_b"}:
            args[param] = int(state.event_id or 1)
        elif param == "x":
            args[param] = int(state.debug_x)
        elif param == "y":
            args[param] = int(state.debug_y)
        elif param in {
            "mip",
            "slice",
            "sample",
            "offset",
            "size",
            "count",
            "max_elements",
            "max_results",
            "max_depth",
            "max_calls",
            "max_events",
            "max_lines",
            "num_frames",
            "capture_delay_ms",
            "context_lines",
            "stride",
            "rt_index",
            "array_index",
            "slot",
            "depth",
            "expand_depth",
            "max_variables",
            "older_than_ms",
            "max_total_bytes",
        }:
            args[param] = 2 if tool == "rd.vfs.tree" and param == "depth" else 0
        elif param == "timeout_ms":
            args[param] = 50 if tool == "rd.debug.run_to" else 0
        elif param in {"view", "instance", "view_index"}:
            args[param] = 0
        elif param == "bins":
            args[param] = 16
        elif param == "stage":
            args[param] = "ps"
        elif param == "stages":
            args[param] = ["vs", "ps"]
        elif param == "types":
            args[param] = ["texture", "buffer"]
        elif param == "channels":
            args[param] = ["r", "g", "b", "a"]
        elif param == "metrics":
            args[param] = ["mse", "max_abs", "psnr"]
        elif param == "metric":
            args[param] = "mse"
        elif param == "mode":
            args[param] = "pixel"
        elif param == "step_mode":
            args[param] = "instruction"
        elif param == "expression":
            args[param] = "0"
        elif tool == "rd.session.update_context" and param == "key":
            args[param] = "notes"
        elif tool == "rd.session.update_context" and param == "value":
            args[param] = f"{state.matrix} contract test"
        elif param == "verbosity":
            args[param] = "short"
        elif param == "marker_policy":
            args[param] = "markers"
        elif param == "name_regex":
            args[param] = "GBuffer|gbuffer"
        elif param == "query":
            args[param] = {"name_contains": "Draw"} if tool.startswith("rd.event.") else "shader"
        elif param == "target":
            if tool in {"rd.shader.compile", "rd.shader.get_disassembly"}:
                args[param] = "ps_5_0"
            elif tool == "rd.debug.run_to":
                args[param] = {"pc": int(state.debug_pc)}
            elif state.debug_target:
                args[param] = dict(state.debug_target)
            else:
                args[param] = {"texture_id": state.texture_id} if state.texture_id else {}
        elif param == "subresource":
            args[param] = {"mip": 0, "slice": 0, "sample": 0}
        elif param == "rect":
            args[param] = {"x": 0, "y": 0, "w": 1, "h": 1}
        elif param == "event_range":
            evt = int(state.event_id or 1)
            args[param] = {"start_event_id": evt, "end_event_id": evt}
        elif param == "pass_range":
            evt = int(state.event_id or 1)
            args[param] = {"begin_event_id": evt, "end_event_id": evt}
        elif param == "history_item":
            args[param] = {"event_id": int(state.event_id or 1), "flags": "Unknown"}
        elif param == "counter_ids":
            args[param] = []
        elif param == "counter_id":
            if state.counter_id is not None:
                args[param] = int(state.counter_id)
        elif param == "layout":
            args[param] = {"stride": 4, "fields": [{"name": "v", "type": "u32", "offset": 0}]}
        elif param == "file_format":
            args[param] = "png"
        elif param == "detail_level":
            args[param] = "full"
        elif param == "connection":
            args[param] = {"pid": 0}
        elif param == "host":
            args[param] = "127.0.0.1"
        elif param == "port":
            args[param] = 38920
        elif param == "option":
            args[param] = "allow_vsync"
        elif param == "value":
            args[param] = True
        elif param == "name":
            args[param] = "contract-test"
        elif param == "state_path":
            args[param] = "topology"
        elif param == "target_value":
            args[param] = "0"
        elif param == "alias":
            args[param] = "contract_alias"
        elif param == "new_name":
            args[param] = "contract_new_name"
        elif param == "config":
            args[param] = {"artifact_dir": str(files["artifacts"])}
        elif param == "pattern":
            args[param] = "00ff"
        elif param == "tex_a":
            args[param] = {"texture_id": state.texture_id, "subresource": {"mip": 0, "slice": 0, "sample": 0}}
        elif param == "tex_b":
            args[param] = {"texture_id": state.texture_id, "subresource": {"mip": 0, "slice": 0, "sample": 0}}
        elif param in {"bundle_spec", "report_spec", "focus", "filter", "expect", "range", "resources", "textures", "options", "capture_options", "overlay_options"}:
            args[param] = {}
        elif param == "replacement":
            args[param] = {"stage": "ps", "shader_id": state.shader_id or "0"}
        elif param == "breakpoints":
            args[param] = [{"pc": int(state.debug_pc)}]
        elif param == "params":
            args[param] = dict(state.debug_params) if state.debug_params else {"x": int(state.debug_x), "y": int(state.debug_y)}
        elif param == "validation":
            args[param] = {"x": int(state.debug_x), "y": int(state.debug_y)}
        elif param == "description":
            args[param] = "contract test"
        elif param == "backend_type":
            args[param] = "local"
        elif param == "project_id":
            args[param] = "contract-test"
        elif param == "fingerprint_type":
            args[param] = "pass"
        elif param == "fingerprint_json":
            args[param] = {"rt_formats": ["RGBA8"], "blend_modes": ["Opaque"], "binding_pattern": ["t0"]}
        elif param == "threshold":
            args[param] = 0.0
        elif param == "device":
            args[param] = {}
        elif param == "cmdline":
            args[param] = ""
        elif param == "exe_path":
            args[param] = "dummy.exe"
        elif param == "working_dir":
            args[param] = str(files["artifacts"])
        elif param == "env":
            args[param] = {}
        elif param == "prefix":
            args[param] = ""
        elif param == "severity_min":
            args[param] = "info"
        elif param == "name_filter":
            args[param] = ""
        elif param == "block_name_or_index":
            args[param] = 0
        elif param == "container":
            args[param] = "dxbc"
        elif param == "entry":
            args[param] = "main"
        elif param == "source":
            args[param] = "float4 main() : SV_Target { return 0; }"
        elif param == "defines":
            args[param] = {}
        elif param in {"include_dirs", "additional_args"}:
            args[param] = []
        elif param == "include_bindings":
            args[param] = True
        elif param.startswith("include_"):
            args[param] = True
        elif param.startswith("max_") or param.startswith("num_"):
            args[param] = 1

    return {k: v for k, v in args.items() if v is not None}


def _update_debug_progress(state: SampleState, payload: dict[str, Any] | None) -> None:
    if not isinstance(payload, dict) or not bool(payload.get("ok")):
        return
    data = _payload_data(payload)
    if not isinstance(data, dict):
        return
    initial_state = data.get("initial_state")
    if isinstance(initial_state, dict):
        try:
            state.debug_pc = int(initial_state.get("pc") or state.debug_pc)
        except Exception:
            pass
    state_payload = data.get("state")
    if isinstance(state_payload, dict):
        try:
            state.debug_pc = int(state_payload.get("pc") or state.debug_pc)
        except Exception:
            pass


def _classify_result(
    tool: str,
    payload: dict[str, Any] | None,
    raw: str,
    exc: str,
    *,
    contract_ok: bool,
    matrix: str,
    transport: str,
    tool_error: str,
) -> tuple[str, str, str, str, str, str]:
    code, message = _coalesce_error(payload, raw, exc)

    has_output = bool(raw.strip() or (exc or "").strip() or message)
    transport_scope = _classify_transport_impact(tool, matrix, transport)
    lower_msg = message.lower()
    lower_exc = str(exc or "").lower()

    if payload is None:
        reason = exc or (raw[:240] if has_output else "non-json response")
        return (
            "blocker",
            reason,
            code or "non_json",
            "structural",
            "tool should emit JSON contract; keep payload parse path aligned with --json output",
            transport_scope,
        )

    if not contract_ok:
        return (
            "blocker",
            "missing canonical contract keys",
            "contract_keys",
            "structural",
            "Ensure tool returns schema_version/tool_version/result_kind/ok/data/artifacts/error",
            transport_scope,
        )

    if bool(payload.get("ok")):
        return "pass", "", "", "", "", transport_scope

    scope_skip = _scope_skip_classification(tool, payload, message, code, transport_scope)
    if scope_skip is not None:
        return scope_skip

    if "unknown session_id" in lower_msg or "no session_id" in lower_msg or "no active session" in lower_msg:
        return (
            "blocker",
            message or "session context missing",
            code or "session_chain",
            "main_chain",
            "rebuild session/replay context and retry with valid session_id/capture_file_id",
            "main chain",
        )

    if "unknown capture_file_id" in lower_msg or "capture file not found" in lower_msg:
        return (
            "blocker",
            message or "capture context missing",
            code or "session_chain",
            "main_chain",
            "rebuild capture context and retry with valid capture_file_id",
            "main chain",
        )

    if _is_session_related_failure(payload) or "session_id" in lower_msg or "capture_file_id" in lower_msg:
        return (
            "blocker",
            message or exc or "session-related contract failure",
            code or "session_chain",
            "main_chain",
            "rebuild session context and retry with fresh capture replay state",
            "main chain",
        )

    return (
        "blocker",
        message or "tool returned ok=false",
        code or "tool_ok_false",
        "structural",
        "Verify args and catalog mapping; align call contract and response payload",
        transport_scope,
    )


async def _invoke_with_repair(
    call_fn: Any,
    tool: str,
    args: dict[str, Any],
    state: SampleState,
    files: dict[str, Path],
) -> tuple[dict[str, Any] | None, str, str]:
    timeout_s = 90.0 if tool == "rd.core.shutdown" else 25.0
    last_payload: dict[str, Any] | None = None
    last_raw = ""
    last_exc = ""

    attempt_args = dict(args)
    for attempt in range(1, SESSION_RETRY_LIMIT + 1):
        _ensure_fixture_inputs(files)
        payload, raw, exc = await call_fn(tool, attempt_args, timeout_s=timeout_s)
        last_payload, last_raw, last_exc = payload, raw, exc

        if payload is not None and not _is_session_related_failure(payload):
            return payload, raw, exc

        if exc and "TaskGroup" in exc:
            return payload, raw, exc

        if not _is_session_related_failure(payload) and not ("session" in (str(exc).lower())):
            return payload, raw, exc

        if attempt >= SESSION_RETRY_LIMIT:
            break

        if _is_session_related_failure(payload) or "session" in (str(exc).lower()):
            await _ensure_context(call_fn, state, files, need_capture=True, need_remote=False)
            attempt_args = dict(args)
            for key in list(attempt_args.keys()):
                if hasattr(state, key):
                    value = getattr(state, key)
                    if value is not None:
                        attempt_args[key] = value

    return last_payload, last_raw, last_exc


def _ordered_tool_names(names: list[str]) -> list[str]:
    tail = [name for name in DESTRUCTIVE_TAIL if name in names]
    tail_set = set(tail)
    ordered = [n for n in names if n and n not in tail_set and n != "rd.remote.disconnect"]
    if "rd.remote.disconnect" in names:
        ordered.append("rd.remote.disconnect")
    return ordered + tail


def _transport_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(items),
        "pass": sum(1 for item in items if item.get("status") == "pass"),
        "issue": sum(1 for item in items if item.get("status") == "issue" and not _is_scope_skip_item(item)),
        "blocker": sum(1 for item in items if item.get("status") == "blocker"),
        "scope_skip": sum(1 for item in items if _is_scope_skip_item(item)),
        "covered_pass": sum(1 for item in items if item.get("status") == "pass" and bool(item.get("covered_scope_skip"))),
        "callable_pass": sum(1 for item in items if bool(item.get("callable"))),
        "contract_pass": sum(1 for item in items if bool(item.get("contract"))),
        "ok_true": sum(1 for item in items if bool(item.get("ok"))),
    }


def _write_markdown_report(result: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    transports = result.get("transports", {})

    lines = [
        "# Tool Contract Report (Dual Sample)",
        "",
        f"- generated_at_utc: {result.get('generated_at_utc', '')}",
        f"- local_rdc: `{result.get('local_rdc', '')}`",
        f"- remote_rdc: `{result.get('remote_rdc', '')}`",
        f"- catalog_tools: {result.get('catalog_count', 0)}",
        "",
    ]

    all_blockers: list[dict[str, Any]] = []
    all_issues: list[dict[str, Any]] = []
    all_scope_skips: list[dict[str, Any]] = []
    for transport_name in ("mcp", "daemon"):
        t_payload = transports.get(transport_name)
        if not isinstance(t_payload, dict):
            continue
        summary = t_payload.get("summary", {})
        lines.extend(
            [
                f"## {transport_name.upper()}",
                f"- total: {summary.get('total', 0)}",
                f"- pass: {summary.get('pass', 0)}",
                f"- issue: {summary.get('issue', 0)}",
                f"- blocker: {summary.get('blocker', 0)}",
                f"- scope_skip: {summary.get('scope_skip', 0)}",
                f"- covered_pass: {summary.get('covered_pass', 0)}",
                f"- callable_pass: {summary.get('callable_pass', 0)}",
                f"- contract_pass: {summary.get('contract_pass', 0)}",
                f"- ok_true: {summary.get('ok_true', 0)}",
                "",
            ],
        )
        fatal = str(t_payload.get("fatal_error") or "").strip()
        if fatal:
            lines.extend(["### Fatal Error", "```text", fatal[:12000], "```", ""])

        cleanup = t_payload.get("cleanup", {})
        if isinstance(cleanup, dict) and cleanup:
            lines.append(f"- cleanup: {json.dumps(cleanup, ensure_ascii=False)}")
            lines.append("")

        items = t_payload.get("items", [])
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                if _is_scope_skip_item(item):
                    all_scope_skips.append(item | {"transport": transport_name})
                    continue
                status = str(item.get("status") or "")
                if status == "blocker":
                    all_blockers.append(item | {"transport": transport_name})
                elif status == "issue":
                    all_issues.append(item | {"transport": transport_name})

    lines.append("## Blockers")
    if not all_blockers:
        lines.append("- (none)")
    else:
        for item in all_blockers:
            lines.append(
                (
                    f"- `{item.get('transport')}` `{item.get('tool')}` ({item.get('matrix')}): "
                    f"{item.get('reason') or 'unknown'}"
                ),
            )
            if item.get("error_code"):
                lines.append(f"  - error_code: `{item.get('error_code')}`")
            if item.get("repro_command"):
                lines.append(f"  - repro: `{item.get('repro_command')}`")
            if item.get("fix_hint"):
                lines.append(f"  - fix: {item.get('fix_hint')}")

    lines.extend(["", "## Scope Skips"])
    if not all_scope_skips:
        lines.append("- (none)")
    else:
        for item in all_scope_skips:
            lines.append(
                (
                    f"- `{item.get('transport')}` `{item.get('tool')}` ({item.get('matrix')}): "
                    f"{item.get('reason') or 'unknown'}"
                ),
            )
            if item.get("issue_type"):
                lines.append(f"  - issue_type: {item.get('issue_type')}")
            if item.get("impact_scope"):
                lines.append(f"  - impact_scope: {item.get('impact_scope')}")
            if item.get("repro_command"):
                lines.append(f"  - repro: `{item.get('repro_command')}`")
            if item.get("fix_hint"):
                lines.append(f"  - fix: {item.get('fix_hint')}")

    lines.extend(["", "## Issues"])
    if not all_issues:
        lines.append("- (none)")
    else:
        for item in all_issues:
            lines.append(
                (
                    f"- `{item.get('transport')}` `{item.get('tool')}` ({item.get('matrix')}): "
                    f"{item.get('reason') or 'unknown'}"
                ),
            )
            if item.get("issue_type"):
                lines.append(f"  - issue_type: {item.get('issue_type')}")
            if item.get("impact_scope"):
                lines.append(f"  - impact_scope: {item.get('impact_scope')}")
            if item.get("repro_command"):
                lines.append(f"  - repro: `{item.get('repro_command')}`")
            if item.get("fix_hint"):
                lines.append(f"  - fix: {item.get('fix_hint')}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _run_transport_mcp(
    root: Path,
    names: list[str],
    params_map: dict[str, list[str]],
    files: dict[str, Path],
    local_rdc: Path,
    remote_rdc: Path,
    *,
    skip_remote: bool = False,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    listed_names: set[str] = set()
    fatal_error = ""
    remote_workflow_events: list[str] = []

    params = StdioServerParameters(
        command=sys.executable,
        args=["mcp/run_mcp.py", "--transport", "stdio", "--log-level", "ERROR"],
        cwd=str(root),
        env=_server_env(),
    )
    states = {
        "local": SampleState(matrix="local", rdc_path=local_rdc),
        "remote": SampleState(matrix="remote", rdc_path=remote_rdc),
    }

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                listed_names = {tool.name for tool in listed.tools}

                async def _call_mcp(
                    name: str,
                    args: dict[str, Any],
                    *,
                    timeout_s: float | None = None,
                ) -> tuple[dict[str, Any] | None, str, str]:
                    effective_timeout_s = _effective_timeout_s(name, args, timeout_s)
                    try:
                        result = await asyncio.wait_for(session.call_tool(name, args), timeout=effective_timeout_s)
                    except Exception as exc:  # noqa: BLE001
                        return None, "", f"call exception: {exc}"
                    text = ""
                    if hasattr(result, "content") and result.content:
                        first = result.content[0]
                        text = getattr(first, "text", str(first))
                    payload = extract_json_payload(text)
                    if payload is None:
                        return None, text, "non-json MCP output"
                    return payload, text, ""

                for name in _ordered_tool_names(names):
                    param_names = params_map.get(name, [])
                    matrix = "remote" if _is_remote_matrix_tool(name, param_names) else "local"
                    state = states[matrix]
                    if skip_remote and matrix == "remote":
                        args = _build_args(name, param_names, states["local"], files)
                        items.append(
                            _make_scope_skip_item(
                                tool=name,
                                transport="mcp",
                                matrix=matrix,
                                reason="local-only mode: remote matrix skipped",
                                issue_type="scope_skip",
                                fix_hint="Run remote workflow in dedicated remote-only smoke pass.",
                                impact_scope="local mode",
                                args=args,
                                error_code="remote_skipped_local_mode",
                                evidence="tool requires remote scope and has been skipped for local-only run",
                                repro_command=f"MCP call `{name}` with args-json `{json.dumps(args, ensure_ascii=False)}`",
                                contract=False,
                            ),
                        )
                        continue
                    requires_capture = ("session_id" in param_names) or ("capture_file_id" in param_names)
                    had_remote_id = bool(state.remote_id)
                    if matrix == "remote" and not state.remote_id:
                        await _ensure_context(_call_mcp, state, files, need_capture=False, need_remote=True)
                        if state.remote_id and not had_remote_id:
                            remote_workflow_events.append(f"mcp-connect:{state.remote_id}")
                    if matrix == "remote" and not state.remote_id and state.remote_reason:
                        args = _build_args(name, param_names, state, files)
                        items.append(
                            {
                                "tool": name,
                                "transport": "mcp",
                                "matrix": matrix,
                                "status": "blocker",
                                "reason": state.remote_reason,
                                "issue_type": state.remote_issue_type or "remote_endpoint",
                                "fix_hint": "Repair rd.remote.connect/rd.remote.ping or the Android bootstrap path before continuing remote smoke.",
                                "impact_scope": "only affects remote_id flow",
                                "ok": False,
                                "callable": False,
                                "contract": True,
                                "args": args,
                                "error_code": state.remote_error_code or "remote_connect_failed",
                                "evidence": state.remote_reason,
                                "repro_command": f"MCP call `{name}` with args-json `{json.dumps(args, ensure_ascii=False)}`",
                            },
                        )
                        continue
                    if "session_id" in param_names and not (state.session_id and state.capture_file_id):
                        await _ensure_context(_call_mcp, state, files, need_capture=True, need_remote=False)
                    elif "capture_file_id" in param_names and not state.capture_file_id:
                        await _ensure_context(_call_mcp, state, files, need_capture=True, need_remote=False)

                    if matrix == "remote":
                        remote_workflow_events.append(f"mcp-tool:{name}")
                    if matrix == "remote" and requires_capture and state.sample_compatibility is False:
                        args = _build_args(name, param_names, state, files)
                        items.append(
                            _make_scope_skip_item(
                                tool=name,
                                transport="mcp",
                                matrix=matrix,
                                reason="sample compatibility issue for remote sample; skip replay-bound tool",
                                issue_type="sample_compatibility",
                                fix_hint="Keep remote matrix replay path as sample-specific; do not mark as remote toolchain failure",
                                impact_scope="only affects remote matrix replay path",
                                args=args,
                                error_code="sample_compatibility",
                                evidence=f"sample_compatibility={state.sample_compatibility}, remote_id={state.remote_id}",
                                repro_command=f"MCP call `{name}` with args-json `{json.dumps(args, ensure_ascii=False)}`",
                                contract=True,
                                sample_compatibility=False,
                            ),
                        )
                        continue

                    covered_pass = _covered_preflight_pass(name, param_names, state)
                    if covered_pass is not None:
                        covered_by_tool, reason, error_code, issue_type, fix_hint, impact_scope = covered_pass
                        args = _build_args(name, param_names, state, files)
                        items.append(
                            _make_covered_pass_item(
                                tool=name,
                                transport="mcp",
                                matrix=matrix,
                                covered_by_tool=covered_by_tool,
                                reason=reason,
                                issue_type=issue_type,
                                fix_hint=fix_hint,
                                impact_scope=impact_scope,
                                args=args,
                                error_code=error_code,
                                evidence=reason,
                                repro_command=f"MCP call `{name}` with args-json `{json.dumps(args, ensure_ascii=False)}`",
                            ),
                        )
                        continue

                    preflight_skip = _preflight_scope_skip(name, param_names, state)
                    if preflight_skip is not None:
                        reason, error_code, issue_type, fix_hint, impact_scope = preflight_skip
                        args = _build_args(name, param_names, state, files)
                        items.append(
                            _make_scope_skip_item(
                                tool=name,
                                transport="mcp",
                                matrix=matrix,
                                reason=reason,
                                issue_type=issue_type,
                                fix_hint=fix_hint,
                                impact_scope=impact_scope,
                                args=args,
                                error_code=error_code,
                                evidence=reason,
                                repro_command=f"MCP call `{name}` with args-json `{json.dumps(args, ensure_ascii=False)}`",
                                contract=True,
                            ),
                        )
                        continue

                    if name == "rd.capture.close_file":
                        await _cleanup_known_sessions(_call_mcp, state)

                    args = _build_args(name, param_names, state, files)
                    payload, raw, exc = await _invoke_with_repair(_call_mcp, name, args, state, files)
                    payload, raw, exc = await _stabilize_destructive_tail_result(_call_mcp, name, args, payload, raw, exc)

                    callable_ok = payload is not None
                    contract_ok = bool(payload is not None and all(key in payload for key in CANONICAL_KEYS))
                    status, reason, error_code, issue_type, fix_hint, impact_scope = _classify_result(
                        name,
                        payload,
                        raw,
                        exc,
                        contract_ok=contract_ok,
                        matrix=matrix,
                        transport="mcp",
                        tool_error=str(payload.get("error_code") if isinstance(payload, dict) else ""),
                    )
                    evidence = (exc or _payload_error(payload)[1] or raw[:500])[:1200]
                    if status == "pass":
                        follow_up = await _validate_success_follow_up(_call_mcp, name, args)
                        if follow_up is not None:
                            status, reason, error_code, issue_type, fix_hint, impact_scope = follow_up
                            evidence = reason[:1200]
                    items.append(
                        {
                            "tool": name,
                            "transport": "mcp",
                            "matrix": matrix,
                            "status": status,
                            "reason": reason,
                            "issue_type": issue_type,
                            "fix_hint": fix_hint,
                            "impact_scope": impact_scope,
                            "sample_compatibility": matrix == "remote" and state.sample_compatibility is False,
                            "ok": bool(payload.get("ok")) if isinstance(payload, dict) else False,
                            "callable": callable_ok,
                            "contract": contract_ok,
                            "args": args,
                            "error_code": error_code,
                            "evidence": evidence,
                            "repro_command": f"MCP call `{name}` with args-json `{json.dumps(args, ensure_ascii=False)}`",
                        },
                    )
                    _track_tool_side_effects(name, args, payload, state)
                    _update_debug_progress(state, payload)

                    if name == "rd.capture.close_replay" and status == "pass":
                        state.session_id = None
                        await _cleanup_known_sessions(_call_mcp, state)
                    if name == "rd.capture.close_file" and status == "pass":
                        state.capture_file_id = None
                        await _cleanup_known_capture_handles(_call_mcp, state)
                    if name == "rd.core.shutdown" and status == "pass":
                        for each_state in states.values():
                            each_state.session_id = None
                            each_state.capture_file_id = None
                    if matrix == "remote" and name == "rd.remote.disconnect":
                        remote_workflow_events.append("mcp-disconnect")

    except Exception:  # noqa: BLE001
        fatal_error = traceback.format_exc()

    missing = [name for name in names if name not in listed_names]
    for name in missing:
        param_names = params_map.get(name, [])
        matrix = "remote" if _is_remote_matrix_tool(name, param_names) else "local"
        items.append(
            {
                "tool": name,
                "transport": "mcp",
                "matrix": matrix,
                "status": "blocker",
                "reason": "not registered in MCP list_tools",
                "issue_type": "structural",
                "fix_hint": f"Sync {_catalog_path().name} against MCP exports and ensure all listed tools are available",
                "impact_scope": "full flow",
                "ok": False,
                "callable": False,
                "contract": False,
                "args": {},
                "error_code": "not_registered",
                "evidence": "not registered in MCP list_tools",
                "repro_command": "python mcp/run_mcp.py --transport stdio",
            },
        )

    by_name: dict[str, dict[str, Any]] = {}
    for item in items:
        by_name[str(item["tool"])] = item
    normalized = [
        by_name.get(
            name,
            {
                "tool": name,
                "transport": "mcp",
                "matrix": "remote" if _is_remote_matrix_tool(name, params_map.get(name, [])) else "local",
                "status": "blocker",
                "reason": "not executed",
                "issue_type": "structural",
                "fix_hint": "Run with transport MCP enabled and confirm full run completes",
                "impact_scope": "main chain",
                "ok": False,
                "callable": False,
                "contract": False,
                "args": {},
                "error_code": "not_executed",
                "evidence": "not executed",
                "repro_command": "python mcp/run_mcp.py --transport stdio",
            },
        )
        for name in names
    ]

    return {
        "registered_count": len(listed_names),
        "fatal_error": fatal_error,
        "cleanup": {"status": "n/a"},
        "remote_workflow_events": remote_workflow_events,
        "items": normalized,
        "summary": _transport_summary(normalized),
    }


async def _run_transport_daemon(
    root: Path,
    names: list[str],
    params_map: dict[str, list[str]],
    files: dict[str, Path],
    local_rdc: Path,
    remote_rdc: Path,
    daemon_context_prefix: str,
    *,
    skip_remote: bool = False,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    fatal_error = ""
    cleanup: dict[str, Any] = {}
    remote_workflow_events: list[str] = []
    context_name = f"{daemon_context_prefix}-{uuid.uuid4().hex[:8]}"
    executor = DaemonExecutor(root=root, context_name=context_name)

    states = {
        "local": SampleState(matrix="local", rdc_path=local_rdc),
        "remote": SampleState(matrix="remote", rdc_path=remote_rdc),
    }

    ok_start, start_detail = await executor.startup()
    if not ok_start:
        fatal_error = f"daemon start failed: {start_detail}"
        cleanup["daemon_started"] = False
        return {
            "registered_count": 0,
            "fatal_error": fatal_error,
            "cleanup": cleanup,
            "items": [],
            "summary": _transport_summary([]),
        }
    cleanup["daemon_started"] = True
    cleanup["daemon_context"] = context_name

    async def _call_daemon(
        name: str,
        args: dict[str, Any],
        *,
        timeout_s: float = 25.0,
    ) -> tuple[dict[str, Any] | None, str, str]:
        return await executor.call_tool(name, args, timeout_s=timeout_s)

    try:
        for name in _ordered_tool_names(names):
            param_names = params_map.get(name, [])
            matrix = "remote" if _is_remote_matrix_tool(name, param_names) else "local"
            state = states[matrix]
            requires_capture = ("session_id" in param_names) or ("capture_file_id" in param_names)
            if skip_remote and matrix == "remote":
                args = _build_args(name, param_names, states["local"], files)
                items.append(
                    _make_scope_skip_item(
                        tool=name,
                        transport="daemon",
                        matrix=matrix,
                        reason="local-only mode: remote matrix skipped",
                        issue_type="scope_skip",
                        fix_hint="Run remote workflow in dedicated remote-only smoke pass.",
                        impact_scope="local mode",
                        args=args,
                        error_code="remote_skipped_local_mode",
                        evidence="tool requires remote scope and has been skipped for local-only run",
                        repro_command=(
                            f"python cli/run_cli.py --daemon-context {context_name} call {name} "
                            f"--args-json '{json.dumps(args, ensure_ascii=False)}' --json --connect"
                        ),
                        contract=False,
                    ),
                )
                continue
            if matrix == "remote":
                remote_workflow_events.append(f"daemon-tool:{name}")
            if matrix == "remote" and not state.remote_id:
                had_remote_id = bool(state.remote_id)
                await _ensure_context(_call_daemon, state, files, need_capture=False, need_remote=True)
                if state.remote_id and not had_remote_id:
                    remote_workflow_events.append(f"daemon-connect:{state.remote_id}")
            if matrix == "remote" and not state.remote_id and state.remote_reason:
                args = _build_args(name, param_names, state, files)
                items.append(
                    {
                        "tool": name,
                        "transport": "daemon",
                        "matrix": matrix,
                        "status": "blocker",
                        "reason": state.remote_reason,
                        "issue_type": state.remote_issue_type or "remote_endpoint",
                        "fix_hint": "Repair rd.remote.connect/rd.remote.ping or the Android bootstrap path before continuing remote smoke.",
                        "impact_scope": "only affects remote_id flow",
                        "ok": False,
                        "callable": False,
                        "contract": True,
                        "args": args,
                        "error_code": state.remote_error_code or "remote_connect_failed",
                        "evidence": state.remote_reason,
                        "repro_command": (
                            f"python cli/run_cli.py --daemon-context {context_name} call {name} "
                            f"--args-json '{json.dumps(args, ensure_ascii=False)}' --json --connect"
                        ),
                    },
                )
                continue
            if "session_id" in param_names and not (state.session_id and state.capture_file_id):
                await _ensure_context(_call_daemon, state, files, need_capture=True, need_remote=False)
            elif "capture_file_id" in param_names and not state.capture_file_id:
                await _ensure_context(_call_daemon, state, files, need_capture=True, need_remote=False)
            if matrix == "remote" and requires_capture and state.sample_compatibility is False:
                args = _build_args(name, param_names, state, files)
                items.append(
                    _make_scope_skip_item(
                        tool=name,
                        transport="daemon",
                        matrix=matrix,
                        reason="sample compatibility issue for remote sample; skip replay-bound tool",
                        issue_type="sample_compatibility",
                        fix_hint="Keep remote matrix replay path as sample-specific; do not mark as remote toolchain failure",
                        impact_scope="only affects remote matrix replay path",
                        args=args,
                        error_code="sample_compatibility",
                        evidence=f"sample_compatibility={state.sample_compatibility}, remote_id={state.remote_id}",
                        repro_command=(
                            f"python cli/run_cli.py --daemon-context {context_name} call {name} "
                            f"--args-json '{json.dumps(args, ensure_ascii=False)}' --json --connect"
                        ),
                        contract=True,
                        sample_compatibility=False,
                    ),
                )
                continue

            covered_pass = _covered_preflight_pass(name, param_names, state)
            if covered_pass is not None:
                covered_by_tool, reason, error_code, issue_type, fix_hint, impact_scope = covered_pass
                args = _build_args(name, param_names, state, files)
                items.append(
                    _make_covered_pass_item(
                        tool=name,
                        transport="daemon",
                        matrix=matrix,
                        covered_by_tool=covered_by_tool,
                        reason=reason,
                        issue_type=issue_type,
                        fix_hint=fix_hint,
                        impact_scope=impact_scope,
                        args=args,
                        error_code=error_code,
                        evidence=reason,
                        repro_command=(
                            f"python cli/run_cli.py --daemon-context {context_name} call {name} "
                            f"--args-json '{json.dumps(args, ensure_ascii=False)}' --json --connect"
                        ),
                    ),
                )
                continue

            preflight_skip = _preflight_scope_skip(name, param_names, state)
            if preflight_skip is not None:
                reason, error_code, issue_type, fix_hint, impact_scope = preflight_skip
                args = _build_args(name, param_names, state, files)
                items.append(
                    _make_scope_skip_item(
                        tool=name,
                        transport="daemon",
                        matrix=matrix,
                        reason=reason,
                        issue_type=issue_type,
                        fix_hint=fix_hint,
                        impact_scope=impact_scope,
                        args=args,
                        error_code=error_code,
                        evidence=reason,
                        repro_command=(
                            f"python cli/run_cli.py --daemon-context {context_name} call {name} "
                            f"--args-json '{json.dumps(args, ensure_ascii=False)}' --json --connect"
                        ),
                        contract=True,
                    ),
                )
                continue

            if name == "rd.capture.close_file":
                await _cleanup_known_sessions(_call_daemon, state)

            args = _build_args(name, param_names, state, files)
            payload, raw, exc = await _invoke_with_repair(_call_daemon, name, args, state, files)
            payload, raw, exc = await _stabilize_destructive_tail_result(_call_daemon, name, args, payload, raw, exc)
            callable_ok = payload is not None
            contract_ok = bool(payload is not None and all(key in payload for key in CANONICAL_KEYS))
            status, reason, error_code, issue_type, fix_hint, impact_scope = _classify_result(
                name,
                payload,
                raw,
                exc,
                contract_ok=contract_ok,
                matrix=matrix,
                transport="daemon",
                tool_error=str(payload.get("error_code") if isinstance(payload, dict) else ""),
            )
            evidence = (exc or _payload_error(payload)[1] or raw[:500])[:1200]
            if status == "pass":
                follow_up = await _validate_success_follow_up(_call_daemon, name, args)
                if follow_up is not None:
                    status, reason, error_code, issue_type, fix_hint, impact_scope = follow_up
                    evidence = reason[:1200]
            items.append(
                {
                    "tool": name,
                    "transport": "daemon",
                    "matrix": matrix,
                    "status": status,
                    "reason": reason,
                    "issue_type": issue_type,
                    "fix_hint": fix_hint,
                    "impact_scope": impact_scope,
                    "sample_compatibility": matrix == "remote" and state.sample_compatibility is False,
                    "ok": bool(payload.get("ok")) if isinstance(payload, dict) else False,
                    "callable": callable_ok,
                    "contract": contract_ok,
                    "args": args,
                    "error_code": error_code,
                    "evidence": evidence,
                    "repro_command": (
                        f"python cli/run_cli.py --daemon-context {context_name} call {name} "
                        f"--args-json '{json.dumps(args, ensure_ascii=False)}' --json --connect"
                    ),
                },
            )
            _track_tool_side_effects(name, args, payload, state)
            _update_debug_progress(state, payload)

            if name == "rd.capture.close_replay" and status == "pass":
                state.session_id = None
                await _cleanup_known_sessions(_call_daemon, state)
            if name == "rd.capture.close_file" and status == "pass":
                state.capture_file_id = None
                await _cleanup_known_capture_handles(_call_daemon, state)
            if name == "rd.core.shutdown" and status == "pass":
                for each_state in states.values():
                    each_state.session_id = None
                    each_state.capture_file_id = None
            if matrix == "remote" and name == "rd.remote.disconnect":
                remote_workflow_events.append("daemon-disconnect")

    except Exception:  # noqa: BLE001
        fatal_error = traceback.format_exc()
    finally:
        stop_ok, stop_detail = await executor.shutdown()
        cleanup["daemon_stop_ok"] = stop_ok
        cleanup["daemon_stop_detail"] = stop_detail

    by_name: dict[str, dict[str, Any]] = {}
    for item in items:
        by_name[str(item["tool"])] = item
    normalized = [
        by_name.get(
            name,
            {
                "tool": name,
                "transport": "daemon",
                "matrix": "remote" if _is_remote_matrix_tool(name, params_map.get(name, [])) else "local",
                "status": "blocker",
                "reason": "not executed",
                "issue_type": "structural",
                "fix_hint": "Run with daemon transport enabled and ensure call path can finish",
                "impact_scope": "main chain",
                "ok": False,
                "callable": False,
                "contract": False,
                "args": {},
                "error_code": "not_executed",
                "evidence": "not executed",
                "repro_command": (
                    f"python cli/run_cli.py --daemon-context {context_name} call {name} "
                    f"--args-json '{{}}' --json --connect"
                ),
            },
        )
        for name in names
    ]

    return {
        "registered_count": len(names),
        "fatal_error": fatal_error,
        "cleanup": cleanup,
        "remote_workflow_events": remote_workflow_events,
        "items": normalized,
        "summary": _transport_summary(normalized),
    }


def _load_catalog() -> tuple[list[str], dict[str, list[str]]]:
    data = json.loads(_catalog_path().read_text(encoding="utf-8"))
    tools = list(data.get("tools", []))
    names = [str(item.get("name", "")).strip() for item in tools]
    declared_count = int(data.get("tool_count") or len(names))
    if len(names) != declared_count:
        raise RuntimeError(f"Catalog tool_count mismatch: declared {declared_count}, got {len(names)}")
    if len(set(names)) != len(names):
        raise RuntimeError("Catalog contains duplicate tool names")
    params_map = {str(item.get("name", "")).strip(): list(item.get("param_names", [])) for item in tools}
    return names, params_map


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dual-sample tool contract checker (catalog-defined tools)")
    parser.add_argument("--local-rdc", required=True)
    parser.add_argument("--remote-rdc", default="")
    parser.add_argument("--transport", choices=["mcp", "daemon", "both"], default="both")
    parser.add_argument("--daemon-context-prefix", default="rdx-smoke")
    parser.add_argument("--skip-remote", action="store_true", help="Skip remote-only tools and remote matrix validation in local smoke mode.")
    parser.add_argument("--artifact-dir", default="", help="Optional artifact root for temporary data.")
    parser.add_argument("--out-json", default="intermediate/logs/tool_contract_report.json")
    parser.add_argument("--out-md", default="intermediate/logs/tool_contract_report.md")
    args = parser.parse_args(argv)
    if (not args.skip_remote) and (not str(args.remote_rdc or "").strip()):
        parser.error("--remote-rdc is required unless --skip-remote is set")
    return args


def main() -> int:
    args = _parse_args()
    root = _tools_root()
    local_rdc = resolve_repo_path(root, args.local_rdc)
    remote_rdc = resolve_repo_path(root, args.remote_rdc) if str(args.remote_rdc or "").strip() else local_rdc

    if args.artifact_dir:
        os.environ["RDX_ARTIFACT_DIR"] = str(resolve_repo_path(root, args.artifact_dir))

    if not local_rdc.is_file():
        print(f"[contract] missing local rdc: {local_rdc}")
        return 2
    if not remote_rdc.is_file():
        if args.skip_remote:
            remote_rdc = local_rdc
            print(f"[contract] remote sample is skipped, using local sample for metadata: {remote_rdc}")
        else:
            print(f"[contract] missing remote rdc: {remote_rdc}")
            return 2
    if args.skip_remote:
        remote_rdc = local_rdc

    names, params_map = _load_catalog()
    files = _prepare_artifacts(root)

    result: dict[str, Any] = {
        "generated_at_utc": _now_iso(),
        "local_rdc": str(local_rdc),
        "remote_rdc": str(remote_rdc),
        "catalog_count": len(names),
        "transports": {},
    }

    if args.transport in {"mcp", "both"}:
        result["transports"]["mcp"] = asyncio.run(
            _run_transport_mcp(
                root,
                names,
                params_map,
                files,
                local_rdc,
                remote_rdc,
                skip_remote=args.skip_remote,
            ),
        )
    if args.transport in {"daemon", "both"}:
        result["transports"]["daemon"] = asyncio.run(
            _run_transport_daemon(
                root,
                names,
                params_map,
                files,
                local_rdc,
                remote_rdc,
                args.daemon_context_prefix,
                skip_remote=args.skip_remote,
            ),
        )

    out_json = resolve_repo_path(root, args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    out_md = resolve_repo_path(root, args.out_md)
    _write_markdown_report(result, out_md)

    print(f"[contract] wrote json: {out_json}")
    print(f"[contract] wrote md: {out_md}")

    has_blocker = False
    for transport_payload in result.get("transports", {}).values():
        if not isinstance(transport_payload, dict):
            continue
        summary = transport_payload.get("summary", {})
        if isinstance(summary, dict) and int(summary.get("blocker", 0)) > 0:
            has_blocker = True
        if str(transport_payload.get("fatal_error") or "").strip():
            has_blocker = True

    return 1 if has_blocker else 0


if __name__ == "__main__":
    raise SystemExit(main())














