#!/usr/bin/env python3
"""Release gate checks for standalone rdx-tools package."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


REQUIRED_DIRS = [
    "rdx",
    "mcp",
    "cli",
    "spec",
    "policy",
    "docs",
    "tests",
    "binaries/windows/x64/pymodules",
    "intermediate/runtime/rdx_cli",
    "intermediate/artifacts",
    "intermediate/pytest",
    "intermediate/logs",
]

REQUIRED_REPORTS = [
    "intermediate/logs/native_smoke_report.md",
    "intermediate/logs/tool_contract_report.md",
]

BANNED_SUFFIXES = {".pdb", ".lib", ".exp", ".ilk", ".h"}


def _tools_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str], cwd: Path) -> tuple[bool, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = proc.returncode == 0
    detail = (proc.stdout or "") + (proc.stderr or "")
    return ok, detail.strip()


def _rg_no_match(pattern: str, cwd: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["rg", "-n", pattern, "."],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # rg: 0=matched, 1=no match, 2=error
    if proc.returncode == 1:
        return True, ""
    if proc.returncode == 0:
        return False, (proc.stdout or "").strip()
    return False, ((proc.stdout or "") + (proc.stderr or "")).strip()


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run release gate checks")
    parser.add_argument("--report", default="intermediate/logs/release_gate_report.md")
    args = parser.parse_args()

    root = _tools_root()
    results: list[tuple[str, bool, str]] = []

    # structure
    for rel in REQUIRED_DIRS:
        p = root / rel
        results.append((f"structure:{rel}", p.is_dir(), "" if p.is_dir() else f"missing {p}"))

    # reference gates
    ext_pattern = "extens" + r"ions[/\\]"
    dbg_pattern = "debug" + "-agent|frame" + "works"
    ok_ext, out_ext = _rg_no_match(ext_pattern, cwd=root)
    results.append(("refs:no_extensions_path", ok_ext, out_ext))
    ok_fw, out_fw = _rg_no_match(dbg_pattern, cwd=root)
    results.append(("refs:no_debug_fw_terms", ok_fw, out_fw))

    # manifest
    ok_manifest, msg_manifest = _check_manifest(root)
    results.append(("manifest:integrity", ok_manifest, msg_manifest))

    # entry checks
    ok_mcp_help, mcp_help = _run([sys.executable, "mcp/run_mcp.py", "--help"], cwd=root)
    results.append(("entry:python mcp/run_mcp.py --help", ok_mcp_help, mcp_help))
    ok_cli_help, cli_help = _run([sys.executable, "cli/run_cli.py", "--help"], cwd=root)
    results.append(("entry:python cli/run_cli.py --help", ok_cli_help, cli_help))
    ok_bat_help, bat_help = _run(["cmd", "/c", "rdx.bat --help"], cwd=root)
    results.append(("entry:rdx.bat --help", ok_bat_help, bat_help))

    # reports
    for rel in REQUIRED_REPORTS:
        p = root / rel
        results.append((f"report:{rel}", p.is_file(), "" if p.is_file() else f"missing {p}"))

    ok_all = all(item[1] for item in results)

    report_path = (root / args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Release Gate Report", ""]
    for name, ok, detail in results:
        lines.append(f"- {'PASS' if ok else 'FAIL'} `{name}`")
        if detail:
            lines.append(f"  - {detail.strip()[:5000]}")
    lines.append("")
    lines.append(f"Overall: {'PASS' if ok_all else 'FAIL'}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[gate] report: {report_path}")
    print(f"[gate] overall: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
