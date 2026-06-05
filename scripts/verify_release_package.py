#!/usr/bin/env python3
"""Verify a self-contained rdx-tools release zip in an extracted path."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from scripts._shared import extract_json_payload


def _cmd_exe() -> str:
    system_root = str(os.environ.get("SystemRoot") or r"C:\Windows")
    return str(Path(system_root) / "System32" / "cmd.exe")


def _run(cmd: list[str], cwd: Path, *, timeout_s: int = 180, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        env=env,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _find_package_root(extract_dir: Path) -> Path:
    candidates = [p for p in extract_dir.iterdir() if p.is_dir() and (p / "rdx.bat").is_file()]
    if len(candidates) != 1:
        raise RuntimeError(f"expected one package root with rdx.bat, found {len(candidates)}")
    return candidates[0]


def _find_fixture(root: Path) -> Path | None:
    fixture_root = root / "tests" / "fixtures"
    if not fixture_root.is_dir():
        return None
    for path in sorted(fixture_root.rglob("*.rdc")):
        if path.is_file():
            return path
    return None


def _verify_doctor(root: Path) -> None:
    env = os.environ.copy()
    env.pop("RDX_PYTHON", None)
    env["RDX_TOOLS_ROOT"] = str(root)
    code, output = _run([_cmd_exe(), "/c", "rdx.bat", "--json", "doctor"], root, env=env)
    payload = extract_json_payload(output)
    if code != 0 or not payload or payload.get("ok") is not True:
        raise RuntimeError(f"doctor failed: exit={code}\n{output}")
    if payload.get("result_kind") != "rdx.doctor":
        raise RuntimeError(f"doctor returned wrong result_kind: {json.dumps(payload)[:500]}")


def _verify_cli_contract(root: Path) -> None:
    env = os.environ.copy()
    env.pop("RDX_PYTHON", None)
    env["RDX_TOOLS_ROOT"] = str(root)
    checks = [
        (["context", "status", "--json"], "rd.session.get_context"),
        (["context", "list", "--json"], "rd.session.list_contexts"),
        (["--daemon-context", "package-contract", "context", "update", "--key", "notes", "--value", "package-verify", "--json"], "rd.session.update_context"),
        (["--daemon-context", "package-contract", "context", "clear", "--json"], "rdx.context.clear"),
        (["vfs", "ls", "--path", "/", "--format", "tsv"], ""),
    ]
    for args, result_kind in checks:
        code, output = _run([_cmd_exe(), "/c", "rdx.bat", *args], root, env=env)
        if code != 0:
            raise RuntimeError(f"contract check failed: rdx.bat {' '.join(args)} exit={code}\n{output}")
        if result_kind:
            payload = extract_json_payload(output)
            if not payload or payload.get("ok") is not True or payload.get("result_kind") != result_kind:
                raise RuntimeError(f"contract check returned wrong payload for {' '.join(args)}:\n{output}")
    negative_checks = [
        (["vfs", "tree", "--path", "/", "--format", "tsv"], "projection_not_supported"),
        (["call", "rd.session.get_context", "--format", "tsv"], "tabular_projection_missing"),
        (["--daemon-context", "package-empty", "diff", "pipeline", "--event-a", "1", "--event-b", "2"], "session_required"),
    ]
    for args, expected in negative_checks:
        code, output = _run([_cmd_exe(), "/c", "rdx.bat", *args], root, env=env)
        payload = extract_json_payload(output)
        code_value = str(((payload or {}).get("error") or {}).get("code") or "")
        if code == 0 or code_value != expected:
            raise RuntimeError(f"negative contract check expected {expected}: rdx.bat {' '.join(args)} exit={code}\n{output}")


def _verify_fixture_smoke(root: Path, *, required: bool) -> None:
    fixture = _find_fixture(root)
    if fixture is None:
        if required:
            raise RuntimeError("missing first-party .rdc fixture in release package")
        return
    if shutil.which("bash") is None:
        if required:
            raise RuntimeError("bash is required to run fixture smoke")
        return
    code, output = _run(
        ["bash", "scripts/smoke_cli.sh", "--rdc", str(fixture), "--context", "package-smoke"],
        root,
        timeout_s=900,
    )
    if code != 0 or "[smoke] PASS" not in output:
        raise RuntimeError(f"fixture smoke failed: exit={code}\n{output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an rdx-tools release package")
    parser.add_argument("--zip", dest="zip_path", required=True, help="Release zip path")
    parser.add_argument("--require-fixture-smoke", action="store_true", help="Fail unless fixture-backed smoke passes")
    args = parser.parse_args(argv)

    zip_path = Path(args.zip_path).resolve()
    if not zip_path.is_file():
        print(f"[verify] missing package: {zip_path}")
        return 2

    temp_dir = Path(tempfile.mkdtemp(prefix="rdx package verify "))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_dir)
        root = _find_package_root(temp_dir)
        _verify_doctor(root)
        _verify_cli_contract(root)
        _verify_fixture_smoke(root, required=bool(args.require_fixture_smoke))
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] {exc}")
        return 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"[verify] PASS: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
