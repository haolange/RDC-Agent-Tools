#!/usr/bin/env python3
"""Run dual-sample smoke + usability checks and generate fixed-path report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from datetime import datetime, timezone


def _tools_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_desktop_rdc(name: str) -> Path:
    return Path.home() / "Desktop" / name


def _run_cmd(
    cmd: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout_s: int = 60,
) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout_s,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@dataclass
class StepResult:
    name: str
    return_code: int
    command: str
    stdout: str = ""
    stderr: str = ""
    detail: str = ""
    allow_blocker: bool = False

    @property
    def success(self) -> bool:
        if self.allow_blocker:
            return self.return_code in (0, 1)
        return self.return_code == 0


def _record_step(steps: list[StepResult], step: StepResult) -> None:
    steps.append(step)


def _safe_print_text(text: str, prefix: str = "") -> None:
    if text is None:
        text = ""
    clean = str(text)
    if prefix:
        clean = f"{prefix}{clean}"
    try:
        print(clean)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        payload = clean.encode(encoding, errors="backslashreplace")
        sys.stdout.buffer.write(payload + b"\n")
        sys.stdout.buffer.flush()


def _check_file(path: Path) -> bool:
    return path.is_file()


def _precheck(
    root: Path,
    local_rdc: Path,
    remote_rdc: Path,
    steps: list[StepResult],
) -> bool:
    ok = True

    if not _check_file(local_rdc):
        _record_step(
            steps,
            StepResult(
                name="precheck-local-rdc",
                return_code=2,
                command=f"test -f {local_rdc}",
                detail=f"missing local sample: {local_rdc}",
            ),
        )
        ok = False
    else:
        _record_step(
            steps,
            StepResult(name="precheck-local-rdc", return_code=0, command=f"test -f {local_rdc}"),
        )

    if not _check_file(remote_rdc):
        _record_step(
            steps,
            StepResult(
                name="precheck-remote-rdc",
                return_code=2,
                command=f"test -f {remote_rdc}",
                detail=f"missing remote sample: {remote_rdc}",
            ),
        )
        ok = False
    else:
        _record_step(
            steps,
            StepResult(name="precheck-remote-rdc", return_code=0, command=f"test -f {remote_rdc}"),
        )

    catalog_cmd = [sys.executable, str(root / "spec" / "validate_catalog.py")]
    code, out, err = _run_cmd(catalog_cmd, cwd=root, timeout_s=90)
    _record_step(
        steps,
        StepResult(
            name="validate_catalog",
            return_code=code,
            command=" ".join(catalog_cmd),
            stdout=out,
            stderr=err,
            detail="validate_catalog failed" if code != 0 else "",
        ),
    )
    if code != 0:
        ok = False

    dll_path = root / "binaries" / "windows" / "x64" / "renderdoc.dll"
    pyd_path = root / "binaries" / "windows" / "x64" / "pymodules" / "renderdoc.pyd"
    if _check_file(dll_path) and _check_file(pyd_path):
        _record_step(
            steps,
            StepResult(
                name="runtime-path-check",
                return_code=0,
                command=f"check files {dll_path}, {pyd_path}",
            ),
        )
    else:
        _record_step(
            steps,
            StepResult(
                name="runtime-path-check",
                return_code=2,
                command=f"check files {dll_path}, {pyd_path}",
                detail="renderdoc.dll/pyd missing",
            ),
        )
        ok = False

    bat = root / "rdx.bat"
    if not _check_file(bat):
        _record_step(
            steps,
            StepResult(
                name="precheck-rdx-bat",
                return_code=2,
                command=f"test -f {bat}",
                detail=f"missing: {bat}",
            ),
        )
        ok = False
    else:
        _record_step(steps, StepResult(name="precheck-rdx-bat", return_code=0, command=f"test -f {bat}"))
        code, out, err = _run_cmd(
            ["cmd", "/c", str(bat), "--non-interactive", "mcp", "--ensure-env"],
            cwd=root,
            timeout_s=60,
        )
        _record_step(
            steps,
            StepResult(
                name="precheck-rdx-bat-ensure-env",
                return_code=code,
                command="rdx.bat --non-interactive mcp --ensure-env",
                stdout=out,
                stderr=err,
                detail="ensure-env failed" if code != 0 else "",
            ),
        )
        if code != 0:
            ok = False

    return ok


def _run_command_smoke(
    root: Path,
    local_rdc: Path,
    remote_rdc: Path,
    cmd_json: str,
    cmd_md: str,
    us_json: str,
    us_md: str,
) -> StepResult:
    cmd = [
        sys.executable,
        str(root / "scripts" / "rdx_bat_command_smoke.py"),
        "--local-rdc",
        str(local_rdc),
        "--remote-rdc",
        str(remote_rdc),
        "--out-json",
        cmd_json,
        "--out-md",
        cmd_md,
        "--out-usability-json",
        us_json,
        "--out-usability-md",
        us_md,
    ]
    code, out, err = _run_cmd(cmd, cwd=root, timeout_s=60 * 8)
    return StepResult(
        name="command-smoke",
        return_code=code,
        command=" ".join(cmd),
        stdout=out,
        stderr=err,
        detail="command smoke blockers found" if code == 1 else ("command smoke failed" if code != 0 and code != 1 else ""),
        allow_blocker=True,
    )


def _run_tool_contract(
    root: Path,
    local_rdc: Path,
    remote_rdc: Path,
    tool_json: str,
    tool_md: str,
) -> StepResult:
    cmd = [
        sys.executable,
        str(root / "scripts" / "tool_contract_check.py"),
        "--local-rdc",
        str(local_rdc),
        "--remote-rdc",
        str(remote_rdc),
        "--transport",
        "both",
        "--out-json",
        tool_json,
        "--out-md",
        tool_md,
    ]
    code, out, err = _run_cmd(cmd, cwd=root, timeout_s=60 * 40)
    return StepResult(
        name="tool-contract",
        return_code=code,
        command=" ".join(cmd),
        stdout=out,
        stderr=err,
        detail=(
            "tool-contract blockers found" if code == 1 else ("tool-contract failed" if code not in {0, 1} else "")
        ),
        allow_blocker=True,
    )


def _run_aggregate(
    root: Path,
    command_json: str,
    tool_json: str,
    usability_json: str,
    out_report: str,
) -> StepResult:
    cmd = [
        sys.executable,
        str(root / "scripts" / "smoke_report_aggregator.py"),
        "--command-json",
        command_json,
        "--tool-json",
        tool_json,
        "--usability-json",
        usability_json,
        "--out",
        out_report,
    ]
    code, out, err = _run_cmd(cmd, cwd=root, timeout_s=60)
    return StepResult(
        name="aggregate",
        return_code=code,
        command=" ".join(cmd),
        stdout=out,
        stderr=err,
        detail="aggregate blockers present" if code == 1 else ("aggregate failed" if code not in {0, 1} else ""),
        allow_blocker=True,
    )


def _collect_daemon_contexts(command_payload: dict[str, Any], tool_payload: dict[str, Any]) -> list[str]:
    contexts: set[str] = set()
    cmd_cleanup = command_payload.get("cleanup", {})
    if isinstance(cmd_cleanup, dict):
        daemons = cmd_cleanup.get("daemons")
        if isinstance(daemons, dict):
            for ctx, started in daemons.items():
                if not isinstance(ctx, str) or not ctx:
                    continue
                if not isinstance(started, bool):
                    continue
                contexts.add(ctx)

    daemon_payload = tool_payload.get("transports", {}).get("daemon", {})
    if isinstance(daemon_payload, dict):
        context = daemon_payload.get("cleanup", {}).get("daemon_context")
        if isinstance(context, str) and context:
            contexts.add(context)

    return sorted(contexts)


def _stop_daemon_contexts(root: Path, bat: Path, contexts: list[str]) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    for ctx in contexts:
        code, out_text, err_text = _run_cmd(
            [
                sys.executable,
                str(root / "cli" / "run_cli.py"),
                "daemon",
                "stop",
                "--daemon-context",
                ctx,
            ],
            cwd=root,
            timeout_s=30,
        )
        out.append((ctx, code == 0, out_text + "\n" + err_text))
    return out


def _kill_residual_python() -> list[str]:
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*run_cli.py*' -or $_.CommandLine -like '*run_mcp.py*' -or $_.CommandLine -like '*intermediate/runtime/rdx_cli*' } | "
        "Select-Object -ExpandProperty Handle"
    )
    pids: list[str] = []
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode == 0:
        for line in (proc.stdout or "").splitlines():
            value = line.strip()
            if value.isdigit():
                pids.append(value)

    stopped: list[str] = []
    for pid in pids:
        stop = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if stop.returncode == 0:
            stopped.append(pid)
    return stopped


def _load_payload(root: Path, relative_path: str) -> dict[str, Any]:
    abs_path = (root / relative_path).resolve()
    if not abs_path.is_file():
        return {}
    try:
        return json.loads(abs_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _append_cleanup_section(
    steps: list[StepResult],
    command_payload: dict[str, Any],
    tool_payload: dict[str, Any],
    bat: Path,
    root: Path,
) -> list[str]:
    residual: list[str] = []
    contexts = _collect_daemon_contexts(command_payload, tool_payload)
    if contexts:
        for ctx, ok, detail in _stop_daemon_contexts(root, bat, contexts):
            if ok:
                residual.append(f"daemon context stopped: {ctx}")
            else:
                residual.append(f"daemon context stop failed: {ctx}; detail={detail[:500]}")
    else:
        residual.append("no context found")

    killed = _kill_residual_python()
    if killed:
        residual.append(f"python process stopped: {', '.join(killed)}")
    return residual


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run dual-sample smoke and rdx.bat usability report")
    parser.add_argument("--local-rdc", default=str(_default_desktop_rdc("03.rdc")))
    parser.add_argument("--remote-rdc", default=str(_default_desktop_rdc("WhiteHair.rdc")))
    parser.add_argument("--out-desktop", default=str(Path.home() / "Desktop" / "rdx_smoke_issues_blockers.md"))
    parser.add_argument("--command-json", default="intermediate/logs/rdx_bat_command_smoke.json")
    parser.add_argument("--command-md", default="intermediate/logs/rdx_bat_command_smoke.md")
    parser.add_argument("--usability-json", default="intermediate/logs/rdx_bat_usability_report.json")
    parser.add_argument("--usability-md", default="intermediate/logs/rdx_bat_usability_report.md")
    parser.add_argument("--tool-json", default="intermediate/logs/tool_contract_report.json")
    parser.add_argument("--tool-md", default="intermediate/logs/tool_contract_report.md")
    return parser.parse_args()


def _print_steps(steps: list[StepResult]) -> None:
    for item in steps:
        mark = "PASS" if item.success else "FAIL"
        if item.stderr:
            _safe_print_text(f"[smoke-196] {mark} {item.name}: {item.command} | {item.detail}")
            _safe_print_text(item.stderr[:400], prefix="")
        else:
            _safe_print_text(f"[smoke-196] {mark} {item.name}: {item.command} | {item.detail}")


def _write_precheck_block_report(
    out_desktop: str,
    steps: list[StepResult],
    local_rdc: Path,
    remote_rdc: Path,
) -> None:
    out_path = Path(out_desktop).resolve()
    passed = sum(1 for item in steps if item.success)
    failed = len(steps) - passed
    lines = [
        "# rdx-tools smoke report (precheck blocked)",
        "",
        f"- generated_at_utc: {datetime.now(timezone.utc).isoformat()}",
        f"- local_rdc: `{local_rdc}`",
        f"- remote_rdc: `{remote_rdc}`",
        f"- total: {len(steps)}",
        f"- pass: {passed}",
        f"- blocker: {failed}",
        "",
        "## Summary",
        "- Precheck not passed (env/files/deps missing or invalid); skipped full smoke test.",
        "",
        "## Failed steps",
    ]

    for item in steps:
        if item.success:
            continue
        lines.append(f"- {item.name}: {item.command}")
        lines.append(f"  - detail: {item.detail or 'precheck failed'}")
        if item.stderr:
            lines.append(f"  - stderr: `{item.stderr[:400]}`")

    lines.extend(["", "## Cleanup status", "- blocked: precheck not passed"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[smoke-196] precheck blocked report: {out_path}")



def _count_blockers_from_payload(payload: dict[str, Any]) -> tuple[int, int]:
    total = 0
    blocker = 0
    for value in payload.values():
        if isinstance(value, dict) and "summary" in value:
            summary = value["summary"]
            if isinstance(summary, dict):
                total += int(summary.get("total", 0))
                blocker += int(summary.get("blocker", 0))
    return total, blocker


def main() -> int:
    args = _parse_args()
    root = _tools_root()
    bat = root / "rdx.bat"
    local_rdc = Path(args.local_rdc)
    remote_rdc = Path(args.remote_rdc)
    steps: list[StepResult] = []

    precheck_ok = _precheck(root, local_rdc, remote_rdc, steps)
    _print_steps(steps)
    if not precheck_ok:
        _write_precheck_block_report(args.out_desktop, steps, local_rdc, remote_rdc)
        print("[smoke-196] FAIL: precheck not passed.")
        return 2

    command_step = _run_command_smoke(root, local_rdc, remote_rdc, args.command_json, args.command_md, args.usability_json, args.usability_md)
    _record_step(steps, command_step)
    if not command_step.success and command_step.return_code > 1:
        print("[smoke-196] command smoke failed unexpectedly")
        return 2

    tool_step = _run_tool_contract(root, local_rdc, remote_rdc, args.tool_json, args.tool_md)
    _record_step(steps, tool_step)
    if not tool_step.success and tool_step.return_code > 1:
        print("[smoke-196] tool contract failed")
        return 2

    aggregate_step = _run_aggregate(root, args.command_json, args.tool_json, args.usability_json, args.out_desktop)
    _record_step(steps, aggregate_step)
    if not aggregate_step.success and aggregate_step.return_code > 1:
        print("[smoke-196] aggregate failed")
        return 2

    command_payload = _load_payload(root, args.command_json)
    tool_payload = _load_payload(root, args.tool_json)
    if not command_payload or not tool_payload:
        print("[smoke-196] FAIL: missing command/tool payload after run")
        return 2

    cleanup_log = _append_cleanup_section(steps, command_payload, tool_payload, bat, root)

    cleanup_ok = all("failed" not in line.lower() for line in cleanup_log)
    print(f"[smoke-196] cleanup_status: {'已清理' if cleanup_ok else '未完全清理（含残留项与手动清理命令）'}")
    for line in cleanup_log:
        print(f"[smoke-196] cleanup: {line}")

    _print_steps(steps)

    _, tool_blockers = _count_blockers_from_payload(tool_payload)
    command_summary = command_payload.get("summary", {})
    command_blockers = int(command_summary.get("blocker", 0))

    if command_blockers > 0 or tool_blockers > 0 or tool_payload.get("transports", {}).get("mcp", {}).get("fatal_error"):
        print(f"[smoke-196] done: FAIL with blockers. desktop report={args.out_desktop}")
        return 1

    print(f"[smoke-196] done: PASS. desktop report={args.out_desktop}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
