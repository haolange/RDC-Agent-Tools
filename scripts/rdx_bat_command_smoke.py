#!/usr/bin/env python3
"""Smoke and usability checks for rdx.bat entry points with dual samples."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _tools_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_desktop_rdc(name: str) -> Path:
    return Path.home() / "Desktop" / name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: float = 30.0,
    stdin_text: str | None = None,
) -> tuple[int, str, str, bool]:
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
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return -1, "", str(exc), True

    return proc.returncode, proc.stdout or "", proc.stderr or "", False


def _trim_output(text: str) -> str:
    return (text or "").strip()


def _extract_json_payload(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _add_result(
    results: list[dict[str, Any]],
    *,
    tool: str,
    command: str,
    matrix: str,
    status: str,
    reason: str,
    error_code: str,
    args: list[str] | dict[str, Any],
    repro_command: str,
    evidence: str,
    issue_type: str | None = None,
    fix_hint: str | None = None,
    impact_scope: str | None = None,
    test_id: str | None = None,
) -> None:
    if test_id is None:
        test_id = command
    results.append(
        {
            "tool": tool,
            "id": test_id,
            "matrix": matrix,
            "command": command,
            "status": status,
            "reason": reason,
            "error_code": error_code,
            "issue_type": issue_type or "",
            "fix_hint": fix_hint or "",
            "impact_scope": impact_scope or "rdx.bat entry path",
            "args": args,
            "repro_command": repro_command,
            "evidence": _trim_output(evidence)[:1200],
        },
    )


def _status_from_checks(
    code: int,
    required_markers: list[str],
    text: str,
    timeout_hit: bool,
    allow_non_zero: bool = False,
) -> tuple[str, str, str]:
    if timeout_hit:
        return "blocker", "command timeout", "timeout"
    lower = (text or "").lower()
    if allow_non_zero:
        if any(token.lower() not in lower for token in required_markers):
            return "issue", "expected output marker missing", "output_missing"
        return "pass", "", ""

    if code != 0:
        return "blocker", f"exit_code={code}", f"exit_{code}"

    missing = [token for token in required_markers if token.lower() not in lower]
    if missing:
        return "issue", f"missing output markers: {', '.join(missing)}", "output_missing"
    return "pass", "", ""


def _run_case(
    results: list[dict[str, Any]],
    root: Path,
    bat: Path,
    *,
    test_id: str,
    matrix: str,
    command: list[str],
    must_contain: list[str] | None = None,
    stdin_text: str | None = None,
    timeout_s: float = 30.0,
    allow_non_zero: bool = False,
    args: list[str] | None = None,
    issue_type: str | None = None,
    fix_hint: str | None = None,
    impact_scope: str | None = None,
) -> tuple[str, int, str]:
    if args is None:
        args = []
    code, out, err, timed_out = _run_command(
        command,
        cwd=root,
        timeout_s=timeout_s,
        stdin_text=stdin_text,
    )
    text = _trim_output(out + "\n" + err)
    cmd_text = " ".join(command)
    status, reason, error_code = _status_from_checks(
        code,
        required_markers=must_contain or [],
        text=text,
        timeout_hit=timed_out,
        allow_non_zero=allow_non_zero,
    )

    _add_result(
        results,
        tool="rdx.bat",
        command=cmd_text,
        matrix=matrix,
        status=status,
        reason=reason,
        error_code=error_code,
        args=args,
        repro_command=cmd_text,
        evidence=text,
        issue_type=issue_type,
        fix_hint=fix_hint,
        impact_scope=impact_scope,
        test_id=test_id,
    )
    return status, code, text


def _run_menu_option_case(
    results: list[dict[str, Any]],
    root: Path,
    bat: Path,
    *,
    case_id: str,
    menu_choice: str,
    required_markers: list[str],
    follow_up_input: str = "0\n",
    issue_type: str | None = None,
    fix_hint: str | None = None,
    impact_scope: str | None = None,
) -> tuple[str, str]:
    code, out, err, timed_out = _run_command(
        ["cmd", "/c", str(bat), "menu"],
        cwd=root,
        stdin_text=f"{menu_choice}\n{follow_up_input}",
        timeout_s=20.0,
    )
    text = _trim_output(out + "\n" + err)
    status, reason, error_code = _status_from_checks(
        code,
        required_markers=required_markers,
        text=text,
        timeout_hit=timed_out,
        allow_non_zero=False,
    )
    _add_result(
        results,
        tool="rdx.bat",
        command="rdx.bat menu",
        matrix="general",
        status=status,
        reason=reason,
        error_code=error_code,
        args=["menu", menu_choice],
        repro_command=f"cmd /c \"{bat}\" menu",
        evidence=text,
        test_id=case_id,
        issue_type=issue_type,
        fix_hint=fix_hint,
        impact_scope=impact_scope,
    )
    return status, text


def _menu_open_test(results: list[dict[str, Any]], root: Path, bat: Path, *, case_id: str = "menu-no-args") -> tuple[str, str]:
    code, out, err, timed_out = _run_command(
        ["cmd", "/c", str(bat)],
        cwd=root,
        stdin_text="0\n",
        timeout_s=20.0,
    )
    text = _trim_output(out + "\n" + err)
    status, reason, error_code = _status_from_checks(
        code,
        required_markers=["Quick Start Menu", "Choose an option"],
        text=text,
        timeout_hit=timed_out,
        allow_non_zero=False,
    )
    _add_result(
        results,
        tool="rdx.bat",
        command="rdx.bat",
        matrix="general",
        status=status,
        reason=reason,
        error_code=error_code,
        args=[],
        repro_command=f"cmd /c \"{bat}\"",
        evidence=text,
        test_id=case_id,
        issue_type="usability" if status != "pass" else "",
        fix_hint="确保 rdx.bat 能在无参数下展示菜单；必要时检查环境变量与依赖提示",
        impact_scope="entry usability",
    )
    return status, text


def _build_usability_overview(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {"pass": 0, "issue": 0, "blocker": 0}
    for item in results:
        summary[item.get("status", "issue")] = summary.get(item.get("status", "issue"), 0) + 1

    required_checks = {
        "menu_no_args": False,
        "menu_env": False,
        "menu_help": False,
        "menu_mcp": False,
        "menu_daemon": False,
        "menu_exit": False,
        "cli_shell": False,
        "daemon_shell": False,
        "capture_open_local": False,
        "help_text_usability": False,
        "unknown_guidance": False,
    }

    checks = []
    for item in results:
        tid = str(item.get("id") or "")
        status = str(item.get("status") or "issue")
        reason = str(item.get("reason") or "")
        entry = {
            "check": tid,
            "status": status,
            "reason": reason,
            "evidence": str(item.get("evidence") or ""),
            "repro_command": str(item.get("repro_command") or ""),
            "impact": item.get("error_code") or "n/a",
        }
        checks.append(entry)

        if tid == "menu-no-args" and status == "pass":
            required_checks["menu_no_args"] = True
        if tid == "menu-env" and status in {"pass", "issue"}:
            required_checks["menu_env"] = True
        if tid == "menu-help" and status == "pass":
            required_checks["menu_help"] = True
        if tid == "menu-mcp" and status == "pass":
            required_checks["menu_mcp"] = True
        if tid == "menu-daemon" and status == "pass":
            required_checks["menu_daemon"] = True
        if tid == "menu-exit" and status == "pass":
            required_checks["menu_exit"] = True
        if tid == "cli-shell-entry" and status == "pass":
            required_checks["cli_shell"] = True
        if tid == "daemon-shell-lifecycle" and status == "pass":
            required_checks["daemon_shell"] = True
        if tid == "capture-open-local-smoke" and status == "pass":
            required_checks["capture_open_local"] = True
        if tid in {"help-long", "help-short"} and status == "pass":
            required_checks["help_text_usability"] = True
        if tid == "unknown-command" and status == "pass":
            required_checks["unknown_guidance"] = True


    critical_missing = [
        name
        for name, ok in required_checks.items()
        if not ok and name in {"menu_no_args", "menu_help", "menu_mcp", "menu_daemon", "menu_exit", "capture_open_local"}
    ]
    overall = "PASS"
    if summary["blocker"] > 0:
        overall = "FAIL"
    elif summary["issue"] > 0 or critical_missing:
        overall = "PARTIAL"

    impact = []
    if critical_missing:
        impact.append(f"missed required usability checks: {', '.join(sorted(set(critical_missing)))}")
    if summary["blocker"] > 0:
        impact.append("blocking behavior exists in entry path")
    if summary["issue"] > 0:
        impact.append("some prompts/commands guidance needs repair")

    return {
        "overall": overall,
        "summary": {
            "total": len(results),
            "pass": summary["pass"],
            "issue": summary["issue"],
            "blocker": summary["blocker"],
        },
        "checks": checks,
        "required_checks": required_checks,
        "impact": impact,
    }


def _write_command_markdown(payload: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    results = payload.get("results", [])
    summary = payload.get("summary", {})
    usability = payload.get("usability", {})
    cleanup = payload.get("cleanup", {})

    lines = [
        "# rdx.bat Command Smoke",
        "",
        f"- generated_at_utc: {payload.get('generated_at_utc', '')}",
        f"- local_rdc: `{payload.get('local_rdc', '')}`",
        f"- remote_rdc: `{payload.get('remote_rdc', '')}`",
        f"- total: {summary.get('total', 0)}",
        f"- pass: {summary.get('pass', 0)}",
        f"- issue: {summary.get('issue', 0)}",
        f"- blocker: {summary.get('blocker', 0)}",
        "",
        "## Usability conclusion",
        f"- status: {usability.get('overall', 'unknown')}",
        f"- impact: {', '.join(usability.get('impact', []) or ['none'])}",
        "",
        "## Blockers",
    ]

    blockers = [item for item in results if isinstance(item, dict) and item.get("status") == "blocker"]
    if not blockers:
        lines.append("- (none)")
    else:
        for item in blockers:
            lines.append(f"- [{item.get('id')}][{item.get('matrix')}][{item.get('command')}] {item.get('reason')}")

    lines.extend(["", "## Issues"])
    issues = [item for item in results if isinstance(item, dict) and item.get("status") == "issue"]
    if not issues:
        lines.append("- (none)")
    else:
        for item in issues:
            lines.append(f"- [{item.get('id')}][{item.get('matrix')}][{item.get('command')}] {item.get('reason')}")

    lines.extend(["", "## Cleanup", f"- {json.dumps(cleanup, ensure_ascii=False)}"])
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_usability_markdown(payload: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    usability = payload.get("usability", {})
    summary = usability.get("summary", {})
    checks = usability.get("checks", [])
    lines = [
        "# rdx.bat usability report",
        "",
        f"- generated_at_utc: {payload.get('generated_at_utc', '')}",
        f"- local_rdc: `{payload.get('local_rdc', '')}`",
        f"- remote_rdc: `{payload.get('remote_rdc', '')}`",
        f"- usability: {usability.get('overall', 'unknown')}",
        f"- total_checks: {summary.get('total', 0)}",
        f"- pass: {summary.get('pass', 0)}",
        f"- issue: {summary.get('issue', 0)}",
        f"- blocker: {summary.get('blocker', 0)}",
        "",
        "## Checks",
    ]
    for item in checks:
        check_id = item.get("check") or "unknown"
        status = item.get("status") or "issue"
        lines.append(f"- {check_id}: {status}")
        reason = str(item.get("reason") or "").strip()
        if reason:
            lines.append(f"  - reason: {reason}")
        repro = str(item.get("repro_command") or "").strip()
        if repro:
            lines.append(f"  - repro: `{repro}`")
    lines.extend(["", "## Impact", f"- {', '.join(usability.get('impact', []) or ['none'])}"])
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test rdx.bat command entry paths")
    parser.add_argument("--local-rdc", default=str(_default_desktop_rdc("03.rdc")))
    parser.add_argument("--remote-rdc", default=str(_default_desktop_rdc("WhiteHair.rdc")))
    parser.add_argument("--out-json", default="intermediate/logs/rdx_bat_command_smoke.json")
    parser.add_argument("--out-md", default="intermediate/logs/rdx_bat_command_smoke.md")
    parser.add_argument(
        "--out-usability-json",
        default="intermediate/logs/rdx_bat_usability_report.json",
    )
    parser.add_argument(
        "--out-usability-md",
        default="intermediate/logs/rdx_bat_usability_report.md",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = _tools_root()
    bat = root / "rdx.bat"
    local_rdc = Path(args.local_rdc)
    remote_rdc = Path(args.remote_rdc)

    results: list[dict[str, Any]] = []
    cleanup: dict[str, Any] = {"daemons": {}}

    if not bat.is_file():
        print(f"[cmd-smoke] missing rdx.bat: {bat}")
        return 2
    if not local_rdc.is_file():
        print(f"[cmd-smoke] missing local rdc: {local_rdc}")
        return 2
    if not remote_rdc.is_file():
        print(f"[cmd-smoke] missing remote rdc: {remote_rdc}")
        return 2

    _menu_open_test(results, root, bat, case_id="menu-no-args")

    _run_case(
        results,
        root,
        bat,
        test_id="help-long",
        matrix="general",
        command=["cmd", "/c", str(bat), "--help"],
        must_contain=["Usage", "rdx.bat"],
        issue_type="usability",
        fix_hint="同步帮助文案中入口示例参数顺序，并确认 `--help` 示例可直接执行",
        impact_scope="rdx.bat usability",
    )
    _run_case(
        results,
        root,
        bat,
        test_id="help-short",
        matrix="general",
        command=["cmd", "/c", str(bat), "-h"],
        must_contain=["Usage", "rdx.bat"],
        issue_type="usability",
        fix_hint="同步帮助文案中入口示例参数顺序，并确认 `-h` 与 `--help` 表达一致",
        impact_scope="rdx.bat usability",
    )
    _run_case(
        results,
        root,
        bat,
        test_id="unknown-command",
        matrix="general",
        command=["cmd", "/c", str(bat), "unknown-command"],
        must_contain=["Try: rdx.bat menu", "rdx.bat cli-shell", "rdx.bat [--non-interactive] daemon-shell"],
        allow_non_zero=True,
        issue_type="usability",
        fix_hint="保持 `未知命令` 提示含明确回退路径，例如 `run menu` 或 `run rdx.bat` 并给出 `menu` 示例",
        impact_scope="rdx.bat usability",
    )
    _run_case(
        results,
        root,
        bat,
        test_id="mcp-ensure-env",
        matrix="general",
        command=["cmd", "/c", str(bat), "--non-interactive", "mcp", "--ensure-env"],
        must_contain=["ok", "true"],
        issue_type="remote_app_dependency",
        fix_hint="补齐运行时依赖（renderdoc.dll/renderdoc.pyd、RenderDoc 安装）并在失败时给明确下一步",
        impact_scope="local env readiness",
    )
    _run_case(
        results,
        root,
        bat,
        test_id="unsupported-cli-help",
        matrix="general",
        command=["cmd", "/c", str(bat), "--non-interactive", "cli", "--help"],
        must_contain=[
            "one-shot CLI entry",
            "Use the following replacements:",
            "rdx.bat cli-shell",
            "daemon-shell",
        ],
        issue_type="usability",
        fix_hint="入口需将一次性 cli 示例替换为 cli-shell 与 daemon-shell + 上下文操作示例",
        impact_scope="rdx.bat usability",
        allow_non_zero=True,
    )

    _run_menu_option_case(
        results,
        root,
        bat,
        case_id="menu-env",
        menu_choice="1",
        required_markers=["Environment", "Quick Start Menu"],
        follow_up_input="N\n0\n",
        issue_type="usability",
        fix_hint="环境页应清晰提示缺失依赖和下一步执行命令",
        impact_scope="rdx.bat usability",
    )
    _run_menu_option_case(
        results,
        root,
        bat,
        case_id="menu-help",
        menu_choice="2",
        required_markers=["Usage", "rdx.bat"],
        issue_type="usability",
        fix_hint="menu 2 的返回内容应能回到主菜单并继续引导到下一个动作",
        impact_scope="rdx.bat usability",
    )
    _run_menu_option_case(
        results,
        root,
        bat,
        case_id="menu-mcp",
        menu_choice="3",
        required_markers=["Choose MCP transport", "Quick Start Menu"],
        issue_type="usability",
        fix_hint="menu 3 启动 MCP 入口必须返回主菜单，避免卡死在子流程",
        impact_scope="rdx.bat usability",
    )
    _run_menu_option_case(
        results,
        root,
        bat,
        case_id="menu-daemon",
        menu_choice="4",
        required_markers=["Daemon Management", "Quick Start Menu"],
        issue_type="usability",
        fix_hint="menu 4 的 daemon management 子菜单应给可执行示例与返回路径",
        impact_scope="rdx.bat usability",
    )
    _run_case(
        results,
        root,
        bat,
        test_id="menu-exit",
        matrix="general",
        command=["cmd", "/c", str(bat), "menu"],
        must_contain=["Quick Start Menu", "Choose an option"],
        stdin_text="0\n",
        timeout_s=20.0,
        issue_type="usability",
        fix_hint="确保 `0` 能快速返回且不产生异常退出",
        impact_scope="rdx.bat usability",
    )

    _run_case(
        results,
        root,
        bat,
        test_id="cli-shell-entry",
        matrix="general",
        command=["cmd", "/c", str(bat), "cli-shell"],
        must_contain=["CLI Window ready", "rdx capture open", "Alias target"],
        issue_type="usability",
        fix_hint="CLI shell 启动提示需明确可执行命令与回到上层路径",
        impact_scope="rdx.bat usability",
    )
    daemon_shell_ctx = "cmd-smoke-daemon-shell"
    daemon_shell_status, _daemon_shell_code, _daemon_shell_text = _run_case(
        results,
        root,
        bat,
        test_id="daemon-shell-lifecycle",
        matrix="general",
        command=["cmd", "/c", str(bat), "daemon-shell", daemon_shell_ctx],
        must_contain=[
            "Daemon Shell",
            "daemon status",
            "daemon stop",
            "exiting daemon shell",
        ],
        stdin_text="1\n\n2\n\n0\n\n",
        timeout_s=45.0,
        issue_type="usability",
        fix_hint="daemon-shell 生命周期测试需验证 `status -> stop -> exit` 闭环和回到上层路径",
        impact_scope="rdx.bat usability",
    )
    cleanup["daemons"][daemon_shell_ctx] = daemon_shell_status == "pass"
    if daemon_shell_status != "pass":
        stop_code, stop_out, stop_err, stop_timeout = _run_command(
            [
                sys.executable,
                str(root / "cli" / "run_cli.py"),
                "daemon",
                "stop",
                "--daemon-context",
                daemon_shell_ctx,
            ],
            cwd=root,
            timeout_s=25.0,
        )
        cleanup["daemon_stop_detail_" + daemon_shell_ctx] = _trim_output(stop_out + "\n" + stop_err)
        cleanup["daemons"][daemon_shell_ctx] = bool(daemon_shell_status == "pass" or (stop_code == 0 and not stop_timeout))

    local_ctx = "cmd-smoke-local"
    capture_input = "\n".join(
        [
            "3",
            f'capture open --file "{local_rdc}" --frame-index 0 --connect',
            "1",
            "2",
            "0",
            "",
        ]
    )
    capture_code, capture_out, capture_err, capture_timed_out = _run_command(
        ["cmd", "/c", str(bat), "daemon-shell", local_ctx],
        cwd=root,
        timeout_s=60.0,
        stdin_text=capture_input,
    )
    capture_text = _trim_output(capture_out + "\n" + capture_err)
    capture_payload = _extract_json_payload(capture_text)

    if capture_timed_out:
        capture_status = "blocker"
        capture_reason = "daemon-shell capture open timed out"
        capture_error_code = "timeout"
    elif capture_code != 0:
        capture_status = "issue"
        capture_reason = f"daemon-shell exit code {capture_code}"
        capture_error_code = f"exit_{capture_code}"
    elif capture_payload is None:
        capture_status = "issue"
        capture_reason = "capture open returned non-json"
        capture_error_code = "non_json"
    elif not bool(capture_payload.get("ok")):
        capture_status = "issue"
        capture_reason = str(capture_payload.get("error_message") or capture_payload.get("error") or "capture open failed")
        capture_error_code = "capture_open_failed"
    else:
        capture_status = "pass"
        capture_reason = ""
        capture_error_code = ""

    _add_result(
        results,
        tool="rdx.bat",
        command="rdx.bat daemon-shell <context> (capture open)",
        matrix="local",
        status=capture_status,
        reason=capture_reason,
        error_code=capture_error_code,
        args=["daemon-shell", local_ctx],
        repro_command=f"cmd /c \"{bat}\" daemon-shell {local_ctx}",
        evidence=capture_text,
        test_id="capture-open-local-smoke",
        issue_type="usability" if capture_status != "pass" else "n/a",
        fix_hint="失败时检查 daemon-shell 启动与 capture 命令参数；确认 RDC 样本路径和 RenderDoc 本地依赖",
        impact_scope="local capture smoke",
    )
    cleanup["daemons"][local_ctx] = capture_status == "pass"
    if capture_status != "pass":
        stop_code, stop_out, stop_err, stop_timeout = _run_command(
            [
                sys.executable,
                str(root / "cli" / "run_cli.py"),
                "daemon",
                "stop",
                "--daemon-context",
                local_ctx,
            ],
            cwd=root,
            timeout_s=25.0,
        )
        cleanup["daemon_stop_detail_" + local_ctx] = _trim_output(stop_out + "\n" + stop_err)
        cleanup["daemons"][local_ctx] = bool(stop_code == 0 and not stop_timeout)

    usability = _build_usability_overview(results)
    payload = {
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
        "cleanup": cleanup,
        "usability": usability,
    }

    out_json = (root / args.out_json).resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    out_md = (root / args.out_md).resolve()
    _write_command_markdown(payload, out_md)

    usability_json = (root / args.out_usability_json).resolve()
    usability_json.parent.mkdir(parents=True, exist_ok=True)
    usability_json.write_text(
        json.dumps(
            {
                "generated_at_utc": payload["generated_at_utc"],
                "usability": usability,
                "local_rdc": payload["local_rdc"],
                "remote_rdc": payload["remote_rdc"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    usability_md = (root / args.out_usability_md).resolve()
    _write_usability_markdown(payload, usability_md)

    print(f"[cmd-smoke] wrote json: {out_json}")
    print(f"[cmd-smoke] wrote md: {out_md}")
    print(f"[cmd-smoke] wrote usability json: {usability_json}")
    print(f"[cmd-smoke] wrote usability md: {usability_md}")
    return 1 if payload["summary"]["blocker"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
