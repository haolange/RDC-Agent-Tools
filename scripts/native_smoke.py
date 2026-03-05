#!/usr/bin/env python3
"""Run native standalone smoke in an isolated sandbox copy of rdx-tools."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


SAMPLE_RDC = Path(r"C:\Users\a1824\Desktop\rdcFiles\TestRdc_Desktop.rdc")


def _tools_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_last_json(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON payload in output")
    return json.loads(stdout[start : end + 1])


def _run_cmd(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _call_cli(bat: Path, random_cwd: Path, operation: str, args: dict[str, Any], *, connect: bool) -> dict[str, Any]:
    run_cli = bat.parent / "cli" / "run_cli.py"
    cmd = [
        sys.executable,
        str(run_cli),
        "call",
        operation,
        "--args-json",
        json.dumps(args, ensure_ascii=False),
        "--json",
    ]
    if connect:
        cmd.append("--connect")
    code, out, err = _run_cmd(cmd, cwd=random_cwd)
    if code != 0:
        raise RuntimeError(f"cli call failed ({operation}): code={code} stderr={err} stdout={out}")
    payload = _parse_last_json(out)
    return payload


def _flatten_actions(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(items: list[dict[str, Any]]) -> None:
        for node in items:
            out.append(node)
            children = node.get("children", [])
            if isinstance(children, list):
                walk([c for c in children if isinstance(c, dict)])

    walk(nodes)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Native smoke check for standalone rdx-tools package")
    parser.add_argument("--rdc", default=str(SAMPLE_RDC))
    args = parser.parse_args()

    src = _tools_root()
    if not src.is_dir():
        raise RuntimeError(f"missing tools root: {src}")
    rdc = Path(args.rdc)
    if not rdc.is_file():
        raise RuntimeError(f"missing rdc file: {rdc}")

    sandbox_parent = Path(tempfile.gettempdir()) / f"rdx_tools_sandbox_{uuid.uuid4().hex[:8]}"
    sandbox = sandbox_parent / "rdx-tools"
    sandbox_parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, sandbox)

    bat = sandbox / "rdx.bat"
    random_cwd = Path(tempfile.gettempdir())

    gate_cmd = ["cmd", "/c", str(bat), "--non-interactive", "mcp", "--ensure-env"]
    gate_code, gate_out, gate_err = _run_cmd(gate_cmd, cwd=random_cwd)

    open_cmd = [
        "cmd",
        "/c",
        str(bat),
        "--non-interactive",
        "cli",
        "capture",
        "open",
        "--file",
        str(rdc),
        "--frame-index",
        "0",
        "--connect",
    ]
    daemon_start = ["cmd", "/c", str(bat), "--non-interactive", "cli", "daemon", "start"]
    daemon_stop = ["cmd", "/c", str(bat), "--non-interactive", "cli", "daemon", "stop"]
    daemon_code, daemon_out, daemon_err = _run_cmd(daemon_start, cwd=random_cwd)
    open_code, open_out, open_err = _run_cmd(open_cmd, cwd=random_cwd)
    open_payload = _parse_last_json(open_out) if open_code == 0 else {}
    session_id = str((open_payload.get("data", {}) if isinstance(open_payload.get("data"), dict) else {}).get("session_id") or "")
    active_event_id = int((open_payload.get("data", {}) if isinstance(open_payload.get("data"), dict) else {}).get("active_event_id") or 0)

    actions_payload = _call_cli(
        bat,
        random_cwd,
        "rd.event.get_actions",
        {"session_id": session_id, "include_markers": True, "include_drawcalls": True},
        connect=True,
    )
    actions = (actions_payload.get("data", {}) if isinstance(actions_payload.get("data"), dict) else {}).get("actions", [])
    flat = _flatten_actions(actions if isinstance(actions, list) else [])
    draw_event = active_event_id
    for item in flat:
        flags = item.get("flags", {})
        if isinstance(flags, dict) and flags.get("is_draw"):
            if isinstance(item.get("event_id"), int):
                draw_event = int(item["event_id"])
                break
    if draw_event > 0:
        _call_cli(bat, random_cwd, "rd.event.set_active", {"session_id": session_id, "event_id": draw_event}, connect=True)

    bindings_payload = _call_cli(bat, random_cwd, "rd.pipeline.get_resource_bindings", {"session_id": session_id}, connect=True)
    bindings = (bindings_payload.get("data", {}) if isinstance(bindings_payload.get("data"), dict) else {}).get("bindings", [])
    usage_payload = _call_cli(bat, random_cwd, "rd.event.get_resource_usage", {"session_id": session_id, "event_id": draw_event}, connect=True)
    usage = (usage_payload.get("data", {}) if isinstance(usage_payload.get("data"), dict) else {}).get("usage", {})
    usage_bindings = usage.get("bindings", []) if isinstance(usage, dict) else []
    mesh_payload = _call_cli(
        bat,
        random_cwd,
        "rd.mesh.get_drawcall_mesh_config",
        {"session_id": session_id, "event_id": draw_event},
        connect=True,
    )
    mesh = (mesh_payload.get("data", {}) if isinstance(mesh_payload.get("data"), dict) else {}).get("mesh_config", {})
    mesh_bindings = mesh.get("bindings", []) if isinstance(mesh, dict) else []
    marker_payload = _call_cli(
        bat,
        random_cwd,
        "rd.macro.find_pass_by_marker",
        {"session_id": session_id, "name_regex": "GBuffer|gbuffer", "max_results": 50},
        connect=True,
    )
    matches = (marker_payload.get("data", {}) if isinstance(marker_payload.get("data"), dict) else {}).get("matches", [])
    marker_hit = False
    if isinstance(matches, list):
        for m in matches:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name", ""))
            path = " > ".join(str(x) for x in (m.get("path") or []) if isinstance(x, str))
            if "RenderGBuffer" in name or "RenderGBuffer" in path:
                marker_hit = True
                break

    stop_code, stop_out, stop_err = _run_cmd(daemon_stop, cwd=random_cwd)

    assertions = [
        ("daemon_start", daemon_code == 0, daemon_err or daemon_out),
        ("ensure_env", gate_code == 0, gate_err or gate_out),
        ("capture_open", open_code == 0 and bool(session_id), open_err or open_out),
        ("resource_bindings_non_empty", isinstance(bindings, list) and len(bindings) > 0, json.dumps(bindings_payload, ensure_ascii=False)[:1200]),
        ("resource_usage_non_empty", isinstance(usage_bindings, list) and len(usage_bindings) > 0, json.dumps(usage_payload, ensure_ascii=False)[:1200]),
        ("mesh_bindings_non_empty", isinstance(mesh_bindings, list) and len(mesh_bindings) > 0, json.dumps(mesh_payload, ensure_ascii=False)[:1200]),
        ("find_pass_by_marker_hit_RenderGBuffer", marker_hit, json.dumps(marker_payload, ensure_ascii=False)[:1200]),
        ("daemon_stop", stop_code == 0, stop_err or stop_out),
    ]

    report = _tools_root() / "intermediate" / "logs" / "native_smoke_report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Native Smoke Report",
        "",
        f"- sandbox: `{sandbox}`",
        f"- rdc: `{rdc}`",
        f"- session_id: `{session_id}`",
        f"- draw_event: `{draw_event}`",
        "",
        "## Assertions",
    ]
    for name, ok, detail in assertions:
        lines.append(f"- {'PASS' if ok else 'FAIL'} `{name}`")
        if detail:
            lines.append(f"  - {detail[:2000]}")
    lines.append("")
    overall = all(x[1] for x in assertions)
    lines.append(f"Overall: {'PASS' if overall else 'FAIL'}")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[smoke] report: {report}")
    print(f"[smoke] overall: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
