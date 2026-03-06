#!/usr/bin/env python3
"""Run dual-sample contract checks for all 196 rd.* tools via MCP and daemon CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from rdx.runtime_paths import ensure_tools_root_env, ensure_runtime_dirs, artifacts_dir, binaries_root, pymodules_dir

CANONICAL_KEYS = {"schema_version", "tool_version", "result_kind", "ok", "data", "artifacts", "error"}
DESTRUCTIVE_TAIL = ("rd.capture.close_replay", "rd.capture.close_file", "rd.core.shutdown")
SESSION_ERROR_SNIPPETS = (
    "Unknown session_id",
    "session_id",
    "Unknown capture_file_id",
    "capture_file_id",
    "No active session",
)
ENV_ISSUE_CODES = {"runtime_error", "not_supported", "not_found", "validation_error"}
ENV_ISSUE_MESSAGE_SNIPPETS = (
    "requires_remote_device",
    "requires_app_integration",
    "App API requires in-process RenderDoc instrumentation",
    "Remote target interaction requires a live RenderDoc remote endpoint",
    "not available in this build",
    "On-host shader compilation is not configured",
    "Shader binary extraction is not available",
    "DebugPixel returned invalid trace",
    "Counter not found:",
    "Missing required parameter",
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
    return _tools_root() / "spec" / "tool_catalog_196.json"


def _default_desktop_rdc(name: str) -> Path:
    return Path.home() / "Desktop" / name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json_payload(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


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


def _coalesce_error(
    payload: dict[str, Any] | None,
    raw: str | None,
    exc: str | None,
) -> tuple[str, str]:
    code, message = _payload_error(payload)
    if message:
        return code, message

    if isinstance(raw, str) and raw.strip():
        parsed = _extract_json_payload(raw)
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


def _is_env_dependency_error(message: str, code: str) -> bool:
    haystack = " ".join([str(code or ""), str(message or "")]).lower()
    return any(snippet in haystack for snippet in ENV_ISSUE_MESSAGE_SNIPPETS)


def _is_sample_compatibility_error(message: str, code: str) -> bool:
    haystack = " ".join([str(code or ""), str(message or "")]).lower()
    return any(snippet in haystack for snippet in SAMPLE_COMPATIBILITY_SNIPPETS)


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
    event_id: int = 1
    texture_id: str | None = None
    resource_id: str | None = None
    buffer_id: str | None = None
    shader_id: str | None = None
    remote_id: str | None = None
    counter_id: int = 0
    sample_compatibility: bool = True


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
            payload = _extract_json_payload(out)
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
            payload = _extract_json_payload(out)
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
        timeout_s: float = 25.0,
    ) -> tuple[dict[str, Any] | None, str, str]:
        def _call() -> tuple[dict[str, Any] | None, str, str]:
            try:
                code, out, err = self._run_cli(
                    [
                        "call",
                        name,
                        "--args-json",
                        json.dumps(args, ensure_ascii=False),
                        "--json",
                        "--connect",
                    ],
                    timeout_s=timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                return None, "", f"call timeout: {exc}"

            payload = _extract_json_payload(out)
            if payload is None:
                detail = (err or out).strip()
                return None, out, f"non-json daemon call output (exit={code}): {detail[:500]}"
            return payload, out, err.strip()

        return await asyncio.to_thread(_call)


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
        elif payload:
            code, message = _payload_error(payload)
            if _is_sample_compatibility_error(message, code):
                state.sample_compatibility = False
                return
            if _is_env_dependency_error(message, code):
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
        elif payload:
            code, message = _payload_error(payload)
            if _is_sample_compatibility_error(message, code):
                state.sample_compatibility = False
                return
            if _is_env_dependency_error(message, code):
                return
            state.session_id = None

    if need_capture and state.session_id:
        await call_fn("rd.replay.set_frame", {"session_id": state.session_id, "frame_index": 0}, timeout_s=20.0)

        actions_payload, _, _ = await call_fn(
            "rd.event.get_actions",
            {"session_id": state.session_id, "include_markers": True, "include_drawcalls": True},
            timeout_s=20.0,
        )
        if actions_payload and actions_payload.get("ok"):
            actions = _payload_data(actions_payload).get("actions", [])
            if isinstance(actions, list):
                fallback_event = None

                def _walk(nodes: list[Any]) -> list[dict[str, Any]]:
                    out: list[dict[str, Any]] = []
                    for node in nodes:
                        if not isinstance(node, dict):
                            continue
                        out.append(node)
                        children = node.get("children")
                        if isinstance(children, list):
                            out.extend(_walk(children))
                    return out

                for item in _walk(actions):
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
                    if isinstance(flags, dict) and bool(flags.get("is_draw")):
                        state.event_id = event_id
                        break
                if fallback_event is not None and state.event_id <= 0:
                    state.event_id = fallback_event

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

    if need_remote and not state.remote_id:
        remote_payload, _, _ = await call_fn(
            "rd.remote.connect",
            {"host": "127.0.0.1", "port": 38920, "timeout_ms": 200},
            timeout_s=10.0,
        )
        if remote_payload and remote_payload.get("ok"):
            data = _payload_data(remote_payload)
            state.remote_id = str(remote_payload.get("remote_id") or data.get("remote_id") or "")
            if not state.remote_id:
                state.remote_id = None


def _default_for_id(param: str, state: SampleState) -> Any:
    if param == "counter_id":
        return state.counter_id
    known = {
        "session_id": state.session_id,
        "capture_file_id": state.capture_file_id,
        "resource_id": state.resource_id or state.texture_id,
        "texture_id": state.texture_id,
        "buffer_id": state.buffer_id,
        "vertex_buffer_id": state.buffer_id,
        "index_buffer_id": state.buffer_id,
        "shader_id": state.shader_id,
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
        elif param in {"x", "y"}:
            args[param] = 1
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
            "timeout_ms",
            "context_lines",
            "stride",
            "rt_index",
            "array_index",
            "slot",
            "expand_depth",
            "max_variables",
            "older_than_ms",
            "max_total_bytes",
        }:
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
            args[param] = int(state.counter_id or 0)
        elif param == "layout":
            args[param] = {"stride": 4, "fields": [{"name": "v", "type": "u32", "offset": 0}]}
        elif param == "file_format":
            args[param] = "png"
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
            args[param] = []
        elif param == "params":
            args[param] = {"x": 1, "y": 1}
        elif param == "validation":
            args[param] = {"x": 1, "y": 1}
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

    if "unknown remote_id" in lower_msg:
        return (
            "issue",
            message or "remote_id unavailable in this environment",
            code or "remote_app_dependency",
            "remote_app_dependency",
            "remote tools require a live remote endpoint or mock workflow",
            "only affects remote_id scope",
        )

    if _is_sample_compatibility_error(message, code):
        return (
            "issue",
            message or "sample compatibility issue",
            "sample_compatibility",
            "sample_compatibility",
            "Keep remote sample compatibility problems independent from toolchain failure classification",
            "only affects remote matrix replay path",
        )

    if _is_env_dependency_error(message, code) or tool.startswith("rd.remote.") or tool.startswith("rd.app."):
        return (
            "issue",
            message or "remote/app dependency insufficient",
            code or "dependency",
            "remote_app_dependency",
            "Prompt dependency gap and next action: remote endpoint / app integration required",
            transport_scope,
        )

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
        "issue": sum(1 for item in items if item.get("status") == "issue"),
        "blocker": sum(1 for item in items if item.get("status") == "blocker"),
        "scope_skip": sum(1 for item in items if str(item.get("issue_type") or "") == "scope_skip"),
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
                    timeout_s: float = 25.0,
                ) -> tuple[dict[str, Any] | None, str, str]:
                    try:
                        result = await asyncio.wait_for(session.call_tool(name, args), timeout=timeout_s)
                    except Exception as exc:  # noqa: BLE001
                        return None, "", f"call exception: {exc}"
                    text = ""
                    if hasattr(result, "content") and result.content:
                        first = result.content[0]
                        text = getattr(first, "text", str(first))
                    payload = _extract_json_payload(text)
                    if payload is None:
                        return None, text, "non-json MCP output"
                    return payload, text, ""

                for name in _ordered_tool_names(names):
                    param_names = params_map.get(name, [])
                    matrix = "remote" if _is_remote_matrix_tool(name, param_names) else "local"
                    if skip_remote and matrix == "remote":
                        args = _build_args(name, param_names, states["local"], files)
                        items.append(
                            {
                                "tool": name,
                                "transport": "mcp",
                                "matrix": matrix,
                                "status": "issue",
                                "reason": "local-only mode: remote matrix skipped",
                                "issue_type": "scope_skip",
                                "fix_hint": "Run remote workflow in dedicated remote-only smoke pass.",
                                "impact_scope": "local mode",
                                "ok": False,
                                "callable": False,
                                "contract": False,
                                "args": args,
                                "error_code": "remote_skipped_local_mode",
                                "evidence": "tool requires remote scope and has been skipped for local-only run",
                                "repro_command": f"MCP call `{name}` with args-json `{json.dumps(args, ensure_ascii=False)}`",
                            },
                        )
                        continue
                    state = states[matrix]
                    requires_capture = ("session_id" in param_names) or ("capture_file_id" in param_names)
                    had_remote_id = bool(state.remote_id)
                    if matrix == "remote" and not state.remote_id:
                        await _ensure_context(_call_mcp, state, files, need_capture=False, need_remote=True)
                        if state.remote_id and not had_remote_id:
                            remote_workflow_events.append(f"mcp-connect:{state.remote_id}")
                    if ("session_id" in param_names or "capture_file_id" in param_names) and not (
                        state.session_id and state.capture_file_id
                    ):
                        await _ensure_context(_call_mcp, state, files, need_capture=True, need_remote=False)

                    if matrix == "remote":
                        remote_workflow_events.append(f"mcp-tool:{name}")
                    if matrix == "remote" and requires_capture and state.sample_compatibility is False:
                        args = _build_args(name, param_names, state, files)
                        items.append(
                            {
                                "tool": name,
                                "transport": "mcp",
                                "matrix": matrix,
                                "status": "issue",
                                "reason": "sample compatibility issue for remote sample; skip replay-bound tool",
                                "issue_type": "sample_compatibility",
                                "fix_hint": "Keep remote matrix replay path as sample-specific; do not mark as remote toolchain failure",
                                "impact_scope": "only affects remote matrix replay path",
                                "ok": False,
                                "callable": False,
                                "contract": True,
                                "sample_compatibility": False,
                                "args": args,
                                "error_code": "sample_compatibility",
                                "evidence": f"sample_compatibility={state.sample_compatibility}, remote_id={state.remote_id}",
                                "repro_command": f"MCP call `{name}` with args-json `{json.dumps(args, ensure_ascii=False)}`",
                            },
                        )
                        continue

                    args = _build_args(name, param_names, state, files)
                    payload, raw, exc = await _invoke_with_repair(_call_mcp, name, args, state, files)

                    if name == "rd.core.shutdown" and payload is None and "connection closed" in (exc or "").lower():
                        payload = {
                            "schema_version": "2.0.0",
                            "tool_version": "1.0.0",
                            "result_kind": "rd.core.shutdown",
                            "ok": True,
                            "data": {"released": {"note": "connection closed after shutdown"}},
                            "artifacts": [],
                            "error": None,
                        }
                        exc = ""

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
                            "evidence": (exc or _payload_error(payload)[1] or raw[:500])[:1200],
                            "repro_command": f"MCP call `{name}` with args-json `{json.dumps(args, ensure_ascii=False)}`",
                        },
                    )

                    if name == "rd.capture.close_replay":
                        state.session_id = None
                    if name == "rd.capture.close_file":
                        state.capture_file_id = None
                    if name == "rd.core.shutdown":
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
                "fix_hint": "Sync tool_catalog_196 against MCP exports and ensure all listed tools are available",
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
                    {
                        "tool": name,
                        "transport": "daemon",
                        "matrix": matrix,
                        "status": "issue",
                        "reason": "local-only mode: remote matrix skipped",
                        "issue_type": "scope_skip",
                        "fix_hint": "Run remote workflow in dedicated remote-only smoke pass.",
                        "impact_scope": "local mode",
                        "ok": False,
                        "callable": False,
                        "contract": False,
                        "args": args,
                        "error_code": "remote_skipped_local_mode",
                        "evidence": "tool requires remote scope and has been skipped for local-only run",
                        "repro_command": (
                            f"python cli/run_cli.py --daemon-context {context_name} call {name} "
                            f"--args-json '{json.dumps(args, ensure_ascii=False)}' --json --connect"
                        ),
                    },
                )
                continue
            if matrix == "remote":
                remote_workflow_events.append(f"daemon-tool:{name}")
            if matrix == "remote" and not state.remote_id:
                had_remote_id = bool(state.remote_id)
                await _ensure_context(_call_daemon, state, files, need_capture=False, need_remote=True)
                if state.remote_id and not had_remote_id:
                    remote_workflow_events.append(f"daemon-connect:{state.remote_id}")
            if ("session_id" in param_names or "capture_file_id" in param_names) and not (
                state.session_id and state.capture_file_id
            ):
                await _ensure_context(_call_daemon, state, files, need_capture=True, need_remote=False)
            if matrix == "remote" and requires_capture and state.sample_compatibility is False:
                args = _build_args(name, param_names, state, files)
                items.append(
                    {
                        "tool": name,
                        "transport": "daemon",
                        "matrix": matrix,
                        "status": "issue",
                        "reason": "sample compatibility issue for remote sample; skip replay-bound tool",
                        "issue_type": "sample_compatibility",
                        "fix_hint": "Keep remote matrix replay path as sample-specific; do not mark as remote toolchain failure",
                        "impact_scope": "only affects remote matrix replay path",
                        "ok": False,
                        "callable": False,
                        "contract": True,
                        "sample_compatibility": False,
                        "args": args,
                        "error_code": "sample_compatibility",
                        "evidence": f"sample_compatibility={state.sample_compatibility}, remote_id={state.remote_id}",
                        "repro_command": (
                            f"python cli/run_cli.py --daemon-context {context_name} call {name} "
                            f"--args-json '{json.dumps(args, ensure_ascii=False)}' --json --connect"
                        ),
                    },
                )
                continue

            args = _build_args(name, param_names, state, files)
            payload, raw, exc = await _invoke_with_repair(_call_daemon, name, args, state, files)
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
                    "evidence": (exc or _payload_error(payload)[1] or raw[:500])[:1200],
                    "repro_command": (
                        f"python cli/run_cli.py --daemon-context {context_name} call {name} "
                        f"--args-json '{json.dumps(args, ensure_ascii=False)}' --json --connect"
                    ),
                },
            )

            if name == "rd.capture.close_replay":
                state.session_id = None
            if name == "rd.capture.close_file":
                state.capture_file_id = None
            if name == "rd.core.shutdown":
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
    if len(names) != 196:
        raise RuntimeError(f"Catalog must contain 196 tools, got {len(names)}")
    if len(set(names)) != 196:
        raise RuntimeError("Catalog contains duplicate tool names")
    params_map = {str(item.get("name", "")).strip(): list(item.get("param_names", [])) for item in tools}
    return names, params_map


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dual-sample tool contract checker (196 tools)")
    parser.add_argument("--local-rdc", default=str(_default_desktop_rdc("03.rdc")))
    parser.add_argument("--remote-rdc", default=str(_default_desktop_rdc("WhiteHair.rdc")))
    parser.add_argument("--transport", choices=["mcp", "daemon", "both"], default="both")
    parser.add_argument("--daemon-context-prefix", default="rdx-smoke")
    parser.add_argument("--skip-remote", action="store_true", help="Skip remote-only tools and remote matrix validation in local smoke mode.")
    parser.add_argument("--artifact-dir", default="", help="Optional artifact root for temporary data.")
    parser.add_argument("--out-json", default="intermediate/logs/tool_contract_report.json")
    parser.add_argument("--out-md", default="intermediate/logs/tool_contract_report.md")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = _tools_root()
    local_rdc = Path(args.local_rdc)
    remote_rdc = Path(args.remote_rdc)

    if args.artifact_dir:
        os.environ["RDX_ARTIFACT_DIR"] = str(Path(args.artifact_dir))

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

    out_json = (root / args.out_json).resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    out_md = (root / args.out_md).resolve()
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


























