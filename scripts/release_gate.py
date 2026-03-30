#!/usr/bin/env python3
"""Release gate checks for standalone rdx-tools package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from rdx.python_runtime import validate_bundled_python_layout
from scripts._shared import load_json, run_subprocess, tools_root, write_text


REQUIRED_DIRS = [
    "rdx",
    "mcp",
    "cli",
    "spec",
    "policy",
    "docs",
    "tests",
    "binaries/windows/x64/python",
    "binaries/windows/x64/pymodules",
    "intermediate/runtime/rdx_cli",
    "intermediate/runtime/worker-cache",
    "intermediate/runtime/worker-state",
    "intermediate/artifacts",
    "intermediate/pytest",
    "intermediate/logs",
]

REQUIRED_FILES = [
    "pyproject.toml",
    "CHANGELOG.md",
]

FIXTURE_DIR = Path("tests/fixtures")
FIXTURE_SUFFIXES = {".rdc"}

CURRENT_REPORTS = [
    "intermediate/logs/rdx_bat_command_smoke.md",
    "intermediate/logs/tool_contract_report.md",
    "intermediate/logs/rdx_smoke_issues_blockers.md",
    "intermediate/logs/rdx_smoke_detailed_report.md",
]
CURRENT_TRUTH_REPORTS = [
    "intermediate/logs/rdx_bat_command_smoke.json",
    "intermediate/logs/tool_contract_report.json",
]

BANNED_SUFFIXES = {".pdb", ".lib", ".exp", ".ilk", ".h"}
TEXT_SCAN_SUFFIXES = {
    ".bat",
    ".cmd",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SCAN_SKIP_PREFIXES = {
    ".git/",
    ".venv/",
    "binaries/windows/x64/manifest.runtime.json",
    "binaries/windows/x64/python/",
    "intermediate/",
    "tests/",
}
USER_DOCS = [
    "README.md",
    "docs/quickstart.md",
    "docs/configuration.md",
    "docs/troubleshooting.md",
    "docs/compatibility-notes.md",
]
USER_PATH_FORBIDDEN_RULES = (
    re.compile(r"\buv\s+sync\b", re.IGNORECASE),
    re.compile(r"python\s+-m\s+venv", re.IGNORECASE),
    re.compile(r"pip\s+install", re.IGNORECASE),
)


@dataclass(frozen=True)
class ScanRule:
    pattern: str
    literal: bool = False


def _tools_root() -> Path:
    return tools_root(__file__)


def _cmd_exe() -> str:
    system_root = str(os.environ.get("SystemRoot") or r"C:\Windows")
    return str(Path(system_root) / "System32" / "cmd.exe")


def _launcher_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("RDX_PYTHON", None)
    return env


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> tuple[bool, str]:
    code, out, err = run_subprocess(cmd, cwd=cwd, env=env)
    ok = code == 0
    detail = (out or "") + (err or "")
    return ok, detail.strip()


def _run_launcher(args: list[str], cwd: Path) -> tuple[bool, str]:
    return _run([_cmd_exe(), "/c", "rdx.bat", *args], cwd, env=_launcher_env())


def _match_line(text: str, rule: ScanRule, *, compiled: re.Pattern[str] | None = None) -> bool:
    if rule.literal:
        return rule.pattern in text
    assert compiled is not None
    return bool(compiled.search(text))


def _python_no_match(rule: ScanRule, cwd: Path) -> tuple[bool, str]:
    compiled = None if rule.literal else re.compile(rule.pattern)
    for path in cwd.rglob("*"):
        if path.is_dir():
            continue
        rel = str(path.relative_to(cwd)).replace("\\", "/")
        if any(rel.startswith(prefix) for prefix in SCAN_SKIP_PREFIXES):
            continue
        if _match_line(rel, rule, compiled=compiled):
            return False, rel
        if path.suffix.lower() not in TEXT_SCAN_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _match_line(line, rule, compiled=compiled):
                return False, f"{rel}:{lineno}: {line.strip()}"
    return True, ""


def _rg_no_match(rule: ScanRule, cwd: Path) -> tuple[bool, str]:
    rg_args = [
        "rg",
        "-n",
        "--glob",
        "!.git/**",
        "--glob",
        "!.venv/**",
        "--glob",
        "!binaries/windows/x64/manifest.runtime.json",
        "--glob",
        "!binaries/windows/x64/python/**",
        "--glob",
        "!intermediate/**",
    ]
    if rule.literal:
        rg_args.append("-F")
    rg_args.extend([rule.pattern, "."])
    try:
        proc = subprocess.run(
            rg_args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        ok, detail = _python_no_match(rule, cwd)
        if ok:
            return True, ""
        return False, f"(python fallback after {exc.__class__.__name__}) {detail}"
    if proc.returncode == 1:
        return True, ""
    if proc.returncode == 0:
        return False, (proc.stdout or "").strip()
    ok, detail = _python_no_match(rule, cwd)
    if ok:
        return True, ""
    rg_detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
    prefix = f"(python fallback after rg exit {proc.returncode}: {rg_detail[:200]})"
    return False, f"{prefix} {detail}".strip()


def _check_manifest(root: Path) -> tuple[bool, str]:
    manifest_path = root / "binaries" / "windows" / "x64" / "manifest.runtime.json"
    if not manifest_path.is_file():
        return False, f"missing manifest: {manifest_path}"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"invalid manifest json: {exc}"
    files = payload.get("files")
    if not isinstance(files, list):
        return False, "manifest files field is missing or invalid"
    bin_root = root / "binaries" / "windows" / "x64"
    for entry in files:
        if not isinstance(entry, dict):
            return False, "manifest entry is not an object"
        rel = str(entry.get("path") or "").strip()
        size = entry.get("size")
        sha = str(entry.get("sha256") or "").strip().lower()
        if not rel:
            return False, "manifest entry has empty path"
        p = bin_root / rel
        if p.suffix.lower() in BANNED_SUFFIXES:
            return False, f"banned file suffix in manifest: {rel}"
        if not p.is_file():
            return False, f"missing runtime file: {rel}"
        if int(p.stat().st_size) != int(size):
            return False, f"size mismatch for {rel}"
        if _sha256(p) != sha:
            return False, f"sha256 mismatch for {rel}"
    return True, f"validated {len(files)} runtime files"


def _check_bundled_python() -> tuple[bool, str]:
    ok, failures, details = validate_bundled_python_layout()
    if ok:
        bundled = details.get("bundled_python") if isinstance(details.get("bundled_python"), dict) else {}
        return True, f"bundled python ready: {bundled.get('python_version', '')}"
    return False, "; ".join(failures)


def _check_user_docs_no_python_bootstrap(root: Path) -> tuple[bool, str]:
    for rel in USER_DOCS:
        path = root / rel
        if not path.is_file():
            return False, f"missing user doc: {path}"
        text = path.read_text(encoding="utf-8")
        for rule in USER_PATH_FORBIDDEN_RULES:
            match = rule.search(text)
            if match:
                line_no = text[: match.start()].count("\n") + 1
                return False, f"{rel}:{line_no}: matched forbidden user-path text: {match.group(0)}"
    return True, "user docs do not require venv or package-manager bootstrap"


def _has_bundled_fixture(root: Path) -> bool:
    fixture_root = root / FIXTURE_DIR
    if not fixture_root.is_dir():
        return False
    for path in fixture_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in FIXTURE_SUFFIXES:
            return True
    return False


def _check_smoke_truth(root: Path) -> tuple[bool, str]:
    command_payload = load_json(root / CURRENT_TRUTH_REPORTS[0])
    if not command_payload:
        return False, f"invalid smoke truth payload: {root / CURRENT_TRUTH_REPORTS[0]}"
    tool_payload = load_json(root / CURRENT_TRUTH_REPORTS[1])
    if not tool_payload:
        return False, f"invalid smoke truth payload: {root / CURRENT_TRUTH_REPORTS[1]}"

    command_summary = command_payload.get("summary")
    if not isinstance(command_summary, dict):
        return False, "invalid rdx_bat_command_smoke.json summary"
    command_blockers = int(command_summary.get("blocker", 0))
    if command_blockers > 0:
        return False, f"command smoke still reports blocker={command_blockers}"

    for transport in ("mcp", "daemon"):
        transport_payload = tool_payload.get("transports", {}).get(transport, {})
        if not isinstance(transport_payload, dict):
            return False, f"missing tool contract transport payload: {transport}"
        fatal_error = str(transport_payload.get("fatal_error") or "").strip()
        if fatal_error:
            return False, f"{transport} tool contract fatal_error: {fatal_error}"
        summary = transport_payload.get("summary")
        if not isinstance(summary, dict):
            return False, f"missing tool contract summary: {transport}"
        blockers = int(summary.get("blocker", 0))
        if blockers > 0:
            return False, f"{transport} tool contract still reports blocker={blockers}"

    return True, "smoke truth reports are current and clean"


def _check_reports(root: Path, *, require_smoke_reports: bool) -> tuple[bool, str]:
    required_artifacts = CURRENT_REPORTS + CURRENT_TRUTH_REPORTS
    missing = [rel for rel in required_artifacts if not (root / rel).is_file()]
    if not missing:
        if require_smoke_reports or _has_bundled_fixture(root):
            return _check_smoke_truth(root)
        return True, "using current smoke reports"

    present = [rel for rel in required_artifacts if rel not in missing]
    if present:
        if require_smoke_reports or _has_bundled_fixture(root):
            return False, f"incomplete smoke reports: missing {', '.join(missing)}"
        return True, "partial smoke reports ignored in clean checkout: full smoke is optional without bundled fixtures"

    if require_smoke_reports or _has_bundled_fixture(root):
        return False, f"missing current reports: {', '.join(missing)}"

    return True, (
        "smoke reports optional in clean checkout: no bundled first-party .rdc fixture; "
        "run release smoke with explicit sample inputs before tagging a release"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run release gate checks")
    parser.add_argument("--report", default="intermediate/logs/release_gate_report.md")
    parser.add_argument(
        "--require-smoke-reports",
        action="store_true",
        help="Fail unless the current smoke reports are present.",
    )
    args = parser.parse_args(argv)

    root = _tools_root()
    results: list[tuple[str, bool, str]] = []

    for rel in REQUIRED_DIRS:
        p = root / rel
        results.append((f"structure:{rel}", p.is_dir(), "" if p.is_dir() else f"missing {p}"))
    for rel in REQUIRED_FILES:
        p = root / rel
        results.append((f"structure:{rel}", p.is_file(), "" if p.is_file() else f"missing {p}"))

    ext_rule = ScanRule(pattern="extensions" + "/", literal=True)
    dbg_rule = ScanRule(pattern="debug" + "-agent|RDC-Agent-" + "Frameworks")
    ok_ext, out_ext = _rg_no_match(ext_rule, cwd=root)
    results.append(("refs:no_extensions_path", ok_ext, out_ext))
    ok_fw, out_fw = _rg_no_match(dbg_rule, cwd=root)
    results.append(("refs:no_debug_fw_terms", ok_fw, out_fw))

    ok_user_docs, user_docs_detail = _check_user_docs_no_python_bootstrap(root)
    results.append(("docs:no_user_python_bootstrap", ok_user_docs, user_docs_detail))

    ok_manifest, msg_manifest = _check_manifest(root)
    results.append(("manifest:integrity", ok_manifest, msg_manifest))
    ok_bundled_python, bundled_python_detail = _check_bundled_python()
    results.append(("manifest:bundled-python", ok_bundled_python, bundled_python_detail))

    ok_bat_help, bat_help = _run_launcher(["--help"], cwd=root)
    results.append(("entry:rdx.bat --help", ok_bat_help, bat_help))
    ok_bat_cli_help, bat_cli_help = _run_launcher(["--non-interactive", "cli", "--help"], cwd=root)
    results.append(("entry:rdx.bat --non-interactive cli --help", ok_bat_cli_help, bat_cli_help))
    ok_bat_mcp_env, bat_mcp_env = _run_launcher(["--non-interactive", "mcp", "--ensure-env"], cwd=root)
    results.append(("entry:rdx.bat --non-interactive mcp --ensure-env", ok_bat_mcp_env, bat_mcp_env))

    ok_mcp_help, mcp_help = _run([sys.executable, "mcp/run_mcp.py", "--help"], cwd=root)
    results.append(("entry:dev-python mcp/run_mcp.py --help", ok_mcp_help, mcp_help))
    ok_cli_help, cli_help = _run([sys.executable, "cli/run_cli.py", "--help"], cwd=root)
    results.append(("entry:dev-python cli/run_cli.py --help", ok_cli_help, cli_help))
    ok_md_health, md_health = _run([sys.executable, "scripts/check_markdown_health.py"], cwd=root)
    results.append(("docs:markdown-health", ok_md_health, md_health))

    ok_reports, report_detail = _check_reports(root, require_smoke_reports=bool(args.require_smoke_reports))
    results.append(("reports:smoke-suite", ok_reports, report_detail))

    ok_all = all(item[1] for item in results)

    report_path = (root / args.report).resolve()
    lines = ["# Release Gate Report", ""]
    for name, ok, detail in results:
        lines.append(f"- {'PASS' if ok else 'FAIL'} `{name}`")
        if detail:
            lines.append(f"  - {detail.strip()[:5000]}")
    lines.append("")
    lines.append(f"Overall: {'PASS' if ok_all else 'FAIL'}")
    write_text(report_path, "\n".join(lines) + "\n")

    print(f"[gate] report: {report_path}")
    print(f"[gate] overall: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
