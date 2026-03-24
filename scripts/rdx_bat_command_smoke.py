#!/usr/bin/env python3
"""Interactive and command smoke checks for rdx.bat."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from rdx.runtime_paths import ensure_tools_root_env
from scripts._shared import extract_json_payload, resolve_repo_path, trim_text, write_text

ReturnCode = tuple[int, str, str, bool]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kill_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except Exception:
        pass


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
) -> ReturnCode:
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        shell=False,
    )
    try:
        out, err = proc.communicate(stdin_text, timeout=max(1, int(timeout_s)))
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired as exc:
        _kill_process_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:
            out = exc.stdout or ""
            err = exc.stderr or ""
        return -1, (out or "") + (exc.stdout or ""), (err or "") + (exc.stderr or ""), True


def _append_result(
    results: list[dict[str, Any]],
    *,
    test_id: str,
    command: str,
    status: str,
    reason: str,
    evidence: str,
    context_id: str = "default",
) -> None:
    results.append(
        {
            "tool": "rdx.bat",
            "id": test_id,
            "status": status,
            "reason": reason,
            "command": command,
            "evidence": trim_text(evidence),
            "context_id": context_id,
        }
    )


def _check_contains(text: str, markers: list[str]) -> tuple[bool, str]:
    for marker in markers:
        if marker not in text:
            return False, marker
    return True, ""


def _cleanup_context(root: Path, context_id: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "cli/run_cli.py", "--daemon-context", context_id, "daemon", "stop"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    detail = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0 or "no active daemon" in detail.lower()
    return ok, trim_text(detail)


def _status_command(root: Path, context_id: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "cli/run_cli.py", "--daemon-context", context_id, "daemon", "status"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _check_timed_start(
    results: list[dict[str, Any]],
    *,
    root: Path,
    test_id: str,
    stdin_text: str,
    context_id: str,
    markers: list[str],
) -> None:
    env = os.environ.copy()
    env["RDX_BAT_TEST_MODE"] = "1"
    code, out, err, timed_out = _run_command(
        ["cmd", "/c", "rdx.bat"],
        cwd=root,
        timeout_s=8.0,
        stdin_text=stdin_text,
        env=env,
    )
    combined = out + "\n" + err
    ok, missing = _check_contains(combined, markers)
    if timed_out and ok:
        _append_result(
            results,
            test_id=test_id,
            command="rdx.bat",
            status="pass",
            reason="",
            evidence=combined,
            context_id=context_id,
        )
    else:
        reason = f"missing marker: {missing}" if not ok else f"unexpected exit code={code}"
        _append_result(
            results,
            test_id=test_id,
            command="rdx.bat",
            status="blocker",
            reason=reason,
            evidence=combined,
            context_id=context_id,
        )


def _build_result_payload(results: list[dict[str, Any]], cleanup: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at_utc": _now_iso(),
        "results": results,
        "summary": {
            "total": len(results),
            "pass": sum(1 for item in results if item.get("status") == "pass"),
            "issue": sum(1 for item in results if item.get("status") == "issue"),
            "blocker": sum(1 for item in results if item.get("status") == "blocker"),
        },
        "cleanup": cleanup,
    }


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    lines = [
        "# rdx.bat command smoke",
        f"- generated_at_utc: {payload.get('generated_at_utc', '')}",
        f"- total: {summary.get('total', 0)}",
        f"- pass: {summary.get('pass', 0)}",
        f"- issue: {summary.get('issue', 0)}",
        f"- blocker: {summary.get('blocker', 0)}",
        "",
        "## Checks",
    ]
    for item in payload.get("results", []):
        lines.append(f"- {item.get('id')}: {item.get('status')}")
        if item.get("reason"):
            lines.append(f"  - reason: {item.get('reason')}")
        lines.append(f"  - command: `{item.get('command')}`")
    lines.append("")
    write_text(path, "\n".join(lines) + "\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke checks for rdx.bat launcher")
    parser.add_argument("--out-json", default="intermediate/logs/rdx_bat_command_smoke.json")
    parser.add_argument("--out-md", default="intermediate/logs/rdx_bat_command_smoke.md")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = ensure_tools_root_env()
    results: list[dict[str, Any]] = []
    cleanup_daemons: dict[str, bool] = {}
    cleanup_notes: dict[str, str] = {}

    env = os.environ.copy()
    env["RDX_BAT_TEST_MODE"] = "1"

    # interactive: help then exit
    code, out, err, timed_out = _run_command(
        ["cmd", "/c", "rdx.bat"],
        cwd=root,
        timeout_s=20.0,
        stdin_text="3\n\n0\n",
        env=env,
    )
    combined = out + "\n" + err
    ok, missing = _check_contains(combined, ["=== rdx.bat Launcher ===", "=== rdx-tools Launcher Help ==="])
    _append_result(
        results,
        test_id="interactive-help",
        command="rdx.bat",
        status="pass" if (not timed_out and code == 0 and ok) else "blocker",
        reason="" if (not timed_out and code == 0 and ok) else (f"missing marker: {missing}" if not ok else f"code={code}, timed_out={timed_out}"),
        evidence=combined,
    )

    # interactive: start CLI default
    code, out, err, timed_out = _run_command(
        ["cmd", "/c", "rdx.bat"],
        cwd=root,
        timeout_s=45.0,
        stdin_text="1\n1\nstatus\nexit\n0\n",
        env=env,
    )
    combined = out + "\n" + err
    status_code, status_text = _status_command(root, "default")
    ok, missing = _check_contains(combined, ["CLI shell ready. context=default", "result_kind\": \"rdx.daemon.status\""])
    _append_result(
        results,
        test_id="interactive-cli-default",
        command="rdx.bat",
        status="pass" if (code == 0 and not timed_out and ok and status_code == 0) else "blocker",
        reason="" if (code == 0 and not timed_out and ok and status_code == 0) else (f"missing marker: {missing}" if not ok else f"status_code={status_code}"),
        evidence=combined + "\n" + status_text,
        context_id="default",
    )

    # interactive: start CLI custom
    custom_ctx = "smoke-cli-custom"
    code, out, err, timed_out = _run_command(
        ["cmd", "/c", "rdx.bat"],
        cwd=root,
        timeout_s=45.0,
        stdin_text=f"1\n2\n{custom_ctx}\nstatus\nclear\nstop\nexit\n0\n",
        env=env,
    )
    combined = out + "\n" + err
    status_code, status_text = _status_command(root, custom_ctx)
    status_payload = extract_json_payload(status_text)
    stopped = (
        status_code == 0
        and isinstance(status_payload, dict)
        and bool(status_payload.get("ok"))
        and isinstance(status_payload.get("data"), dict)
        and status_payload["data"].get("running") is False
    )
    ok, missing = _check_contains(combined, ["CLI shell ready. context=smoke-cli-custom", "result_kind\": \"rdx.context.clear\"", "daemon stopped"])
    _append_result(
        results,
        test_id="interactive-cli-custom",
        command="rdx.bat",
        status="pass" if (code == 0 and not timed_out and ok and stopped) else "blocker",
        reason="" if (code == 0 and not timed_out and ok and stopped) else (f"missing marker: {missing}" if not ok else f"status_code={status_code}, stopped={stopped}"),
        evidence=combined + "\n" + status_text,
        context_id=custom_ctx,
    )

    # interactive: start MCP stdio / http
    _check_timed_start(
        results,
        root=root,
        test_id="interactive-mcp-stdio",
        stdin_text="2\n2\nsmoke-mcp-stdio\n1\n",
        context_id="smoke-mcp-stdio",
        markers=["Start MCP. context=smoke-mcp-stdio", "URL: no URL"],
    )
    _check_timed_start(
        results,
        root=root,
        test_id="interactive-mcp-http",
        stdin_text="2\n2\nsmoke-mcp-http\n2\n127.0.0.1\n8765\n",
        context_id="smoke-mcp-http",
        markers=["Start MCP. context=smoke-mcp-http", "URL: http://127.0.0.1:8765"],
    )

    # non-interactive launcher entrypoints
    for test_id, command, expect_json in [
        ("help", ["cmd", "/c", "rdx.bat", "--help"], False),
        ("mcp-ensure-env", ["cmd", "/c", "rdx.bat", "--non-interactive", "mcp", "--ensure-env"], True),
        ("cli-help", ["cmd", "/c", "rdx.bat", "--non-interactive", "cli", "--help"], True),
        ("cli-daemon-start", ["cmd", "/c", "rdx.bat", "--non-interactive", "cli", "--daemon-context", "smoke-test", "daemon", "start"], True),
        ("cli-daemon-status", ["cmd", "/c", "rdx.bat", "--non-interactive", "cli", "--daemon-context", "smoke-test", "daemon", "status"], True),
        ("cli-daemon-stop", ["cmd", "/c", "rdx.bat", "--non-interactive", "cli", "--daemon-context", "smoke-test", "daemon", "stop"], True),
    ]:
        code, out, err, timed_out = _run_command(command, cwd=root, timeout_s=60.0)
        combined = out + "\n" + err
        payload = extract_json_payload(combined)
        if expect_json:
            passed = (not timed_out) and code == 0 and isinstance(payload, dict) and bool(payload.get("ok"))
        else:
            passed = (not timed_out) and code == 0 and "rdx.bat usage:" in combined
        _append_result(
            results,
            test_id=test_id,
            command=" ".join(command[2:]),
            status="pass" if passed else "blocker",
            reason="" if passed else f"code={code}, timed_out={timed_out}",
            evidence=combined,
        )

    # cleanup
    for ctx in ["default", custom_ctx, "smoke-mcp-stdio", "smoke-mcp-http", "smoke-test"]:
        ok, detail = _cleanup_context(root, ctx)
        cleanup_daemons[ctx] = ok
        cleanup_notes[ctx] = detail

    payload = _build_result_payload(results, {"daemons": cleanup_daemons, "details": cleanup_notes})
    out_json = resolve_repo_path(root, args.out_json)
    out_md = resolve_repo_path(root, args.out_md)
    write_text(out_json, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(out_md, payload)

    print(f"[cmd-smoke] wrote json: {out_json}")
    print(f"[cmd-smoke] wrote md: {out_md}")
    return 1 if any(item.get("status") == "blocker" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
