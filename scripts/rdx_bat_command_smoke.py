#!/usr/bin/env python3
"""Smoke checks for rdx.bat entry chain in local mode."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from rdx.runtime_paths import ensure_tools_root_env

ReturnCode = tuple[int, str, str, bool]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_desktop_rdc(name: str) -> Path:
    return Path.home() / "Desktop" / name


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
) -> ReturnCode:
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout_s)),
            input=stdin_text,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return -1, "", str(exc), True
    return proc.returncode, proc.stdout or "", proc.stderr or "", False


def _trim_text(text: str, limit: int = 4000) -> str:
    value = (text or "").replace("\r", " ").replace("\n", " ").strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


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


def _append_result(
    results: list[dict[str, Any]],
    *,
    tool: str,
    test_id: str,
    matrix: str,
    command: str,
    status: str,
    reason: str,
    error_code: str,
    args: list[str],
    repro_command: str,
    evidence: str,
    issue_type: str,
    impact_scope: str,
    fix_hint: str = "",
    context_id: str = "default",
) -> None:
    results.append(
        {
            "tool": tool,
            "id": test_id,
            "matrix": matrix,
            "command": command,
            "status": status,
            "reason": reason,
            "error_code": error_code,
            "issue_type": issue_type,
            "fix_hint": fix_hint,
            "impact_scope": impact_scope,
            "args": args,
            "repro_command": repro_command,
            "evidence": _trim_text(evidence, 8000),
            "context_id": context_id,
        }
    )


def _add_payload_check(
    results: list[dict[str, Any]],
    *,
    test_id: str,
    command: list[str],
    output: str,
    matrix: str,
    timed_out: bool,
    require_json: bool,
    require_ok: bool = True,
    expected_context: str | None = None,
    allow_non_zero: bool = False,
    issue_type: str = "",
    impact_scope: str = "command-chain",
    fix_hint: str = "",
) -> tuple[str, int]:
    command_text = " ".join(command)
    output_trimmed = _trim_text(output)

    if timed_out:
        _append_result(
            results,
            tool="rdx.bat",
            test_id=test_id,
            matrix=matrix,
            command=command_text,
            status="blocker",
            reason="command timeout",
            error_code="timeout",
            args=[a for a in command[1:]],
            repro_command=command_text,
            evidence=output_trimmed,
            issue_type="",
            impact_scope=impact_scope,
            fix_hint=fix_hint,
        )
        return "blocker", 4

    if allow_non_zero:
        status = "pass" if output else "issue"
        _append_result(
            results,
            tool="rdx.bat",
            test_id=test_id,
            matrix=matrix,
            command=command_text,
            status=status,
            reason="" if status == "pass" else "non-zero but allowed in usability check",
            error_code="" if status == "pass" else "non_zero_output",
            args=[a for a in command[1:]],
            repro_command=command_text,
            evidence=output_trimmed,
            issue_type="usability" if status == "issue" else "",
            impact_scope=impact_scope,
            fix_hint=fix_hint,
        )
        return status, 0 if status == "pass" else 5

    payload = _extract_json_payload(output)
    if require_json and payload is None:
        _append_result(
            results,
            tool="rdx.bat",
            test_id=test_id,
            matrix=matrix,
            command=command_text,
            status="blocker",
            reason="missing structured json payload",
            error_code="non_json",
            args=[a for a in command[1:]],
            repro_command=command_text,
            evidence=output_trimmed,
            issue_type="structural",
            impact_scope=impact_scope,
            fix_hint=fix_hint,
        )
        return "blocker", 5

    if require_json:
        ok = bool(payload.get("ok")) if isinstance(payload, dict) else False
        err_code = str(payload.get("error_code") if isinstance(payload, dict) else "")
        err_msg = str(payload.get("error_message") or "") if isinstance(payload, dict) else ""
        context = str(payload.get("context_id") or "default") if isinstance(payload, dict) else "default"
        if expected_context and context != expected_context:
            _append_result(
                results,
                tool="rdx.bat",
                test_id=test_id,
                matrix=matrix,
                command=command_text,
                status="blocker",
                reason=f"context_id mismatch: expected={expected_context}, actual={context}",
                error_code="context_mismatch",
                args=[a for a in command[1:]],
                repro_command=command_text,
                evidence=output_trimmed,
                issue_type="structural",
                impact_scope=impact_scope,
                fix_hint="Ensure --daemon-context propagates to child commands",
            )
            return "blocker", 5

        if require_ok and not ok:
            _append_result(
                results,
                tool="rdx.bat",
                test_id=test_id,
                matrix=matrix,
                command=command_text,
                status="blocker",
                reason=err_msg or "command failed",
                error_code=err_code or "tool_error",
                args=[a for a in command[1:]],
                repro_command=command_text,
                evidence=output_trimmed,
                issue_type="",
                impact_scope=impact_scope,
                fix_hint=fix_hint,
            )
            return "blocker", 5

        if require_ok and ok:
            _append_result(
                results,
                tool="rdx.bat",
                matrix=matrix,
                test_id=test_id,
                command=command_text,
                status="pass",
                reason="",
                error_code="0",
                args=[a for a in command[1:]],
                repro_command=command_text,
                evidence=output_trimmed,
                issue_type="",
                impact_scope=impact_scope,
                fix_hint=fix_hint,
                context_id=context,
            )
            return "pass", 0

        _append_result(
            results,
            tool="rdx.bat",
            test_id=test_id,
            matrix=matrix,
            command=command_text,
            status="pass",
            reason="",
            error_code="0",
            args=[a for a in command[1:]],
            repro_command=command_text,
            evidence=output_trimmed,
            issue_type="",
            impact_scope=impact_scope,
            fix_hint=fix_hint,
        )
        return "pass", 0

    # Non-json command checks only verify marker presence in output.
    if not output_trimmed:
        _append_result(
            results,
            tool="rdx.bat",
            test_id=test_id,
            matrix=matrix,
            command=command_text,
            status="issue",
            reason="empty output",
            error_code="empty_output",
            args=[a for a in command[1:]],
            repro_command=command_text,
            evidence=output_trimmed,
            issue_type="",
            impact_scope=impact_scope,
            fix_hint=fix_hint,
        )
        return "issue", 5

    _append_result(
        results,
        tool="rdx.bat",
        test_id=test_id,
        matrix=matrix,
        command=command_text,
        status="pass",
        reason="",
        error_code="0",
        args=[a for a in command[1:]],
        repro_command=command_text,
        evidence=output_trimmed,
        issue_type="",
        impact_scope=impact_scope,
        fix_hint=fix_hint,
    )
    return "pass", 0


def _build_usability(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "total": len(results),
        "pass": 0,
        "issue": 0,
        "blocker": 0,
    }
    checks = []
    for item in results:
        status = str(item.get("status") or "issue")
        summary[status] = summary.get(status, 0) + 1
        checks.append(
            {
                "check": str(item.get("id") or "unknown"),
                "status": status,
                "reason": str(item.get("reason") or ""),
                "repro_command": str(item.get("repro_command") or ""),
                "evidence": str(item.get("evidence") or ""),
            }
        )

    if summary["blocker"] > 0:
        overall = "FAIL"
    elif summary["issue"] > 0:
        overall = "PARTIAL"
    else:
        overall = "PASS"

    return {
        "overall": overall,
        "summary": summary,
        "checks": checks,
    }


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = payload["summary"]
    usability = payload["usability"]
    lines = [
        "# rdx.bat command smoke",
        f"- generated_at_utc: {payload.get('generated_at_utc', '')}",
        f"- local_rdc: `{payload.get('local_rdc', '')}`",
        f"- remote_rdc: `{payload.get('remote_rdc', '')}`",
        f"- total: {summary.get('total', 0)}",
        f"- pass: {summary.get('pass', 0)}",
        f"- issue: {summary.get('issue', 0)}",
        f"- blocker: {summary.get('blocker', 0)}",
        f"- usability: {usability.get('overall', 'UNKNOWN')}",
        "",
        "## Checks",
    ]
    for item in usability.get("checks", []):
        lines.append(f"- {item.get('check')}: {item.get('status')}")
        if item.get("reason"):
            lines.append(f"  - reason: {item.get('reason')}")
        if item.get("repro_command"):
            lines.append(f"  - repro: `{item.get('repro_command')}`")

    lines.extend(["", "## Raw payload sample", "```", _trim_text(json.dumps(payload, ensure_ascii=False, indent=2), 12000), "```", ""])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_usability(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    usability = payload["usability"]
    lines = [
        "# rdx.bat usability",
        f"- generated_at_utc: {payload.get('generated_at_utc', '')}",
        f"- usability: {usability.get('overall', 'UNKNOWN')}",
        f"- total: {usability.get('summary', {}).get('total', 0)}",
        f"- pass: {usability.get('summary', {}).get('pass', 0)}",
        f"- issue: {usability.get('summary', {}).get('issue', 0)}",
        f"- blocker: {usability.get('summary', {}).get('blocker', 0)}",
        "",
        "## Checks",
    ]
    for item in usability.get("checks", []):
        lines.append(f"- {item.get('check')}: {item.get('status')}")
        if item.get("reason"):
            lines.append(f"  - reason: {item.get('reason')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke checks for rdx.bat command chain")
    parser.add_argument("--local-rdc", default=str(_default_desktop_rdc("IRP_Desktop.rdc")))
    parser.add_argument("--remote-rdc", default=str(_default_desktop_rdc("IRP_Desktop.rdc")))
    parser.add_argument("--out-json", default="intermediate/logs/rdx_bat_command_smoke.json")
    parser.add_argument("--out-md", default="intermediate/logs/rdx_bat_command_smoke.md")
    parser.add_argument("--out-usability-json", default="intermediate/logs/rdx_bat_usability_report.json")
    parser.add_argument("--out-usability-md", default="intermediate/logs/rdx_bat_usability_report.md")
    return parser.parse_args()


def _build_result_payload(results: list[dict[str, Any]], local_rdc: Path, remote_rdc: Path) -> dict[str, Any]:
    return {
        "generated_at_utc": _now_iso(),
        "local_rdc": str(local_rdc),
        "remote_rdc": str(remote_rdc),
        "results": results,
        "summary": {
            "total": len(results),
            "pass": sum(1 for item in results if item.get("status") == "pass"),
            "issue": sum(1 for item in results if item.get("status") == "issue"),
            "blocker": sum(1 for item in results if item.get("status") == "blocker"),
        },
    }


def _cleanup_sections(results: list[dict[str, Any]], context_ids: list[str]) -> dict[str, Any]:
    return {
        "contexts": context_ids,
        "context_count": len(context_ids),
        "commands": {
            "rdx.bat --help": any(item.get("id") == "rdx-help" for item in results),
            "mcp-ensure-env": any(item.get("id") == "mcp-ensure-env" for item in results),
            "cli-shell": any(item.get("id") == "cli-shell-help" for item in results),
            "daemon-shell": any(item.get("id") == "daemon-shell-lifecycle" for item in results),
        },
    }


def main() -> int:
    args = _parse_args()
    root = ensure_tools_root_env()

    bat = root / "rdx.bat"
    local_rdc = Path(args.local_rdc)
    remote_rdc = Path(args.remote_rdc)

    if not bat.is_file():
        print(f"[cmd-smoke] missing rdx.bat: {bat}")
        return 2
    if not local_rdc.is_file():
        print(f"[cmd-smoke] missing local rdc: {local_rdc}")
        return 2
    if not remote_rdc.is_file():
        print(f"[cmd-smoke] missing remote rdc: {remote_rdc}")
        return 2

    results: list[dict[str, Any]] = []
    context_smoke = f"smoke-{int(datetime.now(timezone.utc).timestamp())}"

    _, _, _, has_blocker = False, None, None, False

    code, out, err, timed_out = _run_command(
        ["cmd", "/c", str(bat), "--help"],
        cwd=root,
        timeout_s=20.0,
    )
    status, code_out = _add_payload_check(
        results,
        test_id="rdx-help",
        command=["rdx.bat", "--help"],
        output=(out + "\n" + err),
        matrix="general",
        timed_out=timed_out,
        require_json=False,
        issue_type="usability",
        impact_scope="entry command",
        fix_hint="Ensure --help shows non-interactive mcp and shell lifecycle entries",
    )
    if status == "blocker":
        has_blocker = True

    mcp_ctx = f"{context_smoke}-mcp"
    code, out, err, timed_out = _run_command(
        ["cmd", "/c", str(bat), "--non-interactive", "mcp", "--daemon-context", mcp_ctx, "--ensure-env"],
        cwd=root,
        timeout_s=60.0,
    )
    status, code_out = _add_payload_check(
        results,
        test_id="mcp-ensure-env",
        command=["rdx.bat", "--non-interactive", "mcp", "--daemon-context", mcp_ctx, "--ensure-env"],
        output=(out + "\n" + err),
        matrix="local",
        timed_out=timed_out,
        require_json=True,
        expected_context=mcp_ctx,
        impact_scope="mcp lifecycle",
        fix_hint="Run with fixed renderdoc runtime: check binaries/windows/x64/renderdoc.dll and pymodules/renderdoc.pyd",
    )
    if status == "blocker":
        has_blocker = True

    code, out, err, timed_out = _run_command(
        ["cmd", "/c", str(bat), "--non-interactive", "cli-shell", "--daemon-context", f"{context_smoke}-cli", "--help"],
        cwd=root,
        timeout_s=30.0,
    )
    status, code_out = _add_payload_check(
        results,
        test_id="cli-shell-help",
        command=["rdx.bat", "--non-interactive", "cli-shell", "--help"],
        output=(out + "\n" + err),
        matrix="general",
        timed_out=timed_out,
        require_json=False,
        issue_type="usability",
        impact_scope="cli shell",
        fix_hint="Use non-interactive mode for smoke; avoid blocking interactive shell input.",
    )
    if status == "blocker":
        has_blocker = True

    daemon_ctx = f"{context_smoke}-daemon"
    code, out, err, timed_out = _run_command(
        ["cmd", "/c", str(bat), "--non-interactive", "daemon-shell", "--daemon-context", daemon_ctx, "start", "status", "stop"],
        cwd=root,
        timeout_s=60.0,
    )
    status, code_out = _add_payload_check(
        results,
        test_id="daemon-shell-lifecycle",
        command=[
            "rdx.bat",
            "--non-interactive",
            "daemon-shell",
            "--daemon-context",
            daemon_ctx,
            "start",
            "status",
            "stop",
        ],
        output=(out + "\n" + err),
        matrix="general",
        timed_out=timed_out,
        require_json=True,
        expected_context=daemon_ctx,
        impact_scope="daemon lifecycle",
        fix_hint="daemon-shell should complete start/status/stop and emit JSON on timeout-safe path",
    )
    if status == "blocker":
        has_blocker = True

    payload = _build_result_payload(results, local_rdc, remote_rdc)
    payload["usability"] = _build_usability(results)
    payload["cleanup"] = _cleanup_sections(results, [mcp_ctx, daemon_ctx])

    out_json = (root / args.out_json).resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    out_md = (root / args.out_md).resolve()
    _write_markdown(out_md, payload)

    usability = {
        "generated_at_utc": payload["generated_at_utc"],
        "local_rdc": payload["local_rdc"],
        "remote_rdc": payload["remote_rdc"],
        "usability": payload["usability"],
    }
    out_usability_json = (root / args.out_usability_json).resolve()
    out_usability_json.parent.mkdir(parents=True, exist_ok=True)
    out_usability_json.write_text(json.dumps(usability, ensure_ascii=False, indent=2), encoding="utf-8")

    out_usability_md = (root / args.out_usability_md).resolve()
    _write_usability(out_usability_md, usability)

    print(f"[cmd-smoke] wrote json: {out_json}")
    print(f"[cmd-smoke] wrote md: {out_md}")
    print(f"[cmd-smoke] wrote usability json: {out_usability_json}")
    print(f"[cmd-smoke] wrote usability md: {out_usability_md}")

    return 1 if has_blocker else 0


if __name__ == "__main__":
    raise SystemExit(main())




