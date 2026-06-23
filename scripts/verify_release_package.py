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


PUBLIC_COMMAND = "rdx"
WINDOWS_LAUNCHER_FILE = "rdx.bat"
EXPECTED_PUBLIC_COMMANDS = [PUBLIC_COMMAND]
EXPECTED_ENTRYPOINTS = [WINDOWS_LAUNCHER_FILE, "bin/rdx", "cli/run_cli.py"]
REMOVED_CATALOG_TOOLS = {"rd.resource.rename", "rd.shader.save_binary"}
MCP_DOC_MARKERS = (
    "mcp/run_mcp.py",
    "model context protocol",
    "mcp server",
    "mcp public",
)

TEXT_SUFFIXES = {
    ".bat",
    ".cmd",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PRE_GA_PATH_MARKERS = (
    "rdx/" + "runtime_" + "materializer.py",
    "intermediate/runtime/" + "worker" + "-cache",
)
PRE_GA_TEXT_MARKERS = (
    "worker_" + "materialize",
    "runtime_" + "owner",
    "owner_" + "lease",
    "runtime_" + "baton",
    "active_" + "baton",
    "rehydrate_" + "status",
    "staged_" + "handoff",
    "runtime_" + "parallelism_" + "ceiling",
    "claim_" + "runtime_" + "owner",
    "release_" + "runtime_" + "owner",
    "export_" + "runtime_" + "baton",
    "rehydrate_" + "runtime_" + "baton",
)


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


def _public_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("RDX_PYTHON", None)
    env["RDX_TOOLS_ROOT"] = str(root)
    env["PATH"] = str(root) + os.pathsep + str(env.get("PATH") or "")
    pathext = str(env.get("PATHEXT") or "")
    if ".BAT" not in pathext.upper().split(";"):
        env["PATHEXT"] = pathext + (";" if pathext else "") + ".BAT"
    return env


def _run_public(root: Path, args: list[str], *, timeout_s: int = 180) -> tuple[int, str]:
    return _run([_cmd_exe(), "/c", PUBLIC_COMMAND, *args], root, timeout_s=timeout_s, env=_public_env(root))


def _run_windows_launcher_file(root: Path, args: list[str], *, timeout_s: int = 180) -> tuple[int, str]:
    env = os.environ.copy()
    env.pop("RDX_PYTHON", None)
    env["RDX_TOOLS_ROOT"] = str(root)
    return _run([_cmd_exe(), "/c", WINDOWS_LAUNCHER_FILE, *args], root, timeout_s=timeout_s, env=env)


def _expect_public_error(root: Path, args: list[str], expected_codes: set[str]) -> None:
    code, output = _run_public(root, args)
    payload = extract_json_payload(output)
    code_value = str(((payload or {}).get("error") or {}).get("code") or "")
    if code == 0 or code_value not in expected_codes:
        expected = ", ".join(sorted(expected_codes))
        raise RuntimeError(f"negative contract check expected one of {expected}: {PUBLIC_COMMAND} {' '.join(args)} exit={code} code={code_value}\n{output}")


def _find_package_root(extract_dir: Path) -> Path:
    candidates = [p for p in extract_dir.iterdir() if p.is_dir() and (p / WINDOWS_LAUNCHER_FILE).is_file()]
    if len(candidates) != 1:
        raise RuntimeError(f"expected one package root with {WINDOWS_LAUNCHER_FILE}, found {len(candidates)}")
    return candidates[0]


def _verify_release_manifest(root: Path) -> None:
    manifest_path = root / "RELEASE_MANIFEST.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"missing release manifest: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("public_commands") != EXPECTED_PUBLIC_COMMANDS:
        raise RuntimeError(f"release manifest public_commands mismatch: {payload.get('public_commands')!r}")
    entrypoints = payload.get("entrypoints")
    if sorted(entrypoints or []) != sorted(EXPECTED_ENTRYPOINTS):
        raise RuntimeError(f"release manifest entrypoints mismatch: {entrypoints!r}")
    files = payload.get("files")
    if not isinstance(files, list):
        raise RuntimeError("release manifest files field is missing or invalid")
    paths = {str(item.get("path") or "") for item in files if isinstance(item, dict)}
    missing_entrypoints = [entry for entry in EXPECTED_ENTRYPOINTS if entry not in paths or not (root / entry).is_file()]
    if missing_entrypoints:
        raise RuntimeError(f"release manifest missing physical entrypoints: {missing_entrypoints}")
    leaked_mcp = sorted(path for path in paths if path == "mcp" or path.startswith("mcp/"))
    if leaked_mcp:
        raise RuntimeError(f"release manifest exposes MCP paths: {leaked_mcp[:5]}")
    leaked_tests = sorted(path for path in paths if path == "tests" or path.startswith("tests/"))
    if leaked_tests:
        raise RuntimeError(f"release manifest exposes test paths: {leaked_tests[:5]}")
    leaked_rdc = sorted(path for path in paths if path.lower().endswith(".rdc"))
    if leaked_rdc:
        raise RuntimeError(f"release manifest exposes .rdc fixtures: {leaked_rdc[:5]}")


def _verify_license_inventory(root: Path) -> None:
    inventory_path = root / "LICENSE_INVENTORY.json"
    sbom_path = root / "SBOM.json"
    if not inventory_path.is_file():
        raise RuntimeError(f"missing license inventory: {inventory_path}")
    if not sbom_path.is_file():
        raise RuntimeError(f"missing SBOM: {sbom_path}")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if not isinstance(inventory, list):
        raise RuntimeError("license inventory is not a list")
    project_rows = [row for row in inventory if isinstance(row, dict) and row.get("name") == "rdx-tools"]
    if not project_rows:
        raise RuntimeError("license inventory missing rdx-tools row")
    if project_rows[0].get("license") != "Apache-2.0" or project_rows[0].get("path") != "LICENSE":
        raise RuntimeError(f"rdx-tools license inventory mismatch: {project_rows[0]!r}")
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    components = sbom.get("components") if isinstance(sbom, dict) else None
    if not isinstance(components, list):
        raise RuntimeError("SBOM components field is missing or invalid")
    sbom_rows = [row for row in components if isinstance(row, dict) and row.get("name") == "rdx-tools"]
    if not sbom_rows or sbom_rows[0].get("license") != "Apache-2.0":
        raise RuntimeError("SBOM does not report Apache-2.0 for rdx-tools")


def _verify_doctor(root: Path) -> None:
    code, output = _run_public(root, ["--json", "doctor"])
    payload = extract_json_payload(output)
    if code != 0 or not payload or payload.get("ok") is not True:
        raise RuntimeError(f"doctor failed: exit={code}\n{output}")
    if payload.get("result_kind") != "rdx.doctor":
        raise RuntimeError(f"doctor returned wrong result_kind: {json.dumps(payload)[:500]}")


def _verify_physical_launcher_file(root: Path) -> None:
    code, output = _run_windows_launcher_file(root, ["--non-interactive", "--json", "doctor"])
    payload = extract_json_payload(output)
    if code != 0 or not payload or payload.get("ok") is not True:
        raise RuntimeError(f"windows launcher file doctor failed: exit={code}\n{output}")
    if payload.get("result_kind") != "rdx.doctor":
        raise RuntimeError(f"windows launcher file returned wrong result_kind: {json.dumps(payload)[:500]}")


def _verify_version_payload(root: Path) -> None:
    code, output = _run_public(root, ["version", "--json"])
    payload = extract_json_payload(output)
    data = payload.get("data") if isinstance(payload, dict) else {}
    if code != 0 or not payload or payload.get("ok") is not True or not isinstance(data, dict):
        raise RuntimeError(f"version --json failed: exit={code}\n{output}")
    if data.get("public_commands") != EXPECTED_PUBLIC_COMMANDS:
        raise RuntimeError(f"version public_commands mismatch: {data.get('public_commands')!r}")
    entrypoints = data.get("entrypoints")
    if not isinstance(entrypoints, dict) or "windows_bat" not in entrypoints or "posix_shell" not in entrypoints or "python_cli" not in entrypoints:
        raise RuntimeError(f"version entrypoints missing physical launchers: {json.dumps(data)[:500]}")


def _verify_tools_catalog(root: Path) -> None:
    code, output = _run_public(root, ["tools", "list", "--json"])
    payload = extract_json_payload(output)
    data = payload.get("data") if isinstance(payload, dict) else {}
    if code != 0 or not payload or payload.get("ok") is not True or not isinstance(data, dict):
        raise RuntimeError(f"tools list failed: exit={code}\n{output}")
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("tools list returned no tools array")
    if int(data.get("tool_count") or len(tools)) != 194:
        raise RuntimeError(f"expected 194 active tools, got {data.get('tool_count')!r}")
    names = {str(item.get("name") or "") for item in tools if isinstance(item, dict)}
    leaked = sorted(REMOVED_CATALOG_TOOLS & names)
    if leaked:
        raise RuntimeError(f"removed aliases still listed: {leaked}")


def _verify_cli_contract(root: Path) -> None:
    checks = [
        (["context", "status", "--json"], "rd.session.get_context"),
        (["context", "list", "--json"], "rd.session.list_contexts"),
        (["--daemon-context", "package-contract", "context", "update", "--key", "notes", "--value", "package-verify", "--json"], "rd.session.update_context"),
        (["--daemon-context", "package-contract", "context", "clear", "--json"], "rdx.context.clear"),
        (["vfs", "ls", "--path", "/", "--format", "tsv"], ""),
    ]
    for args, result_kind in checks:
        code, output = _run_public(root, args)
        if code != 0:
            raise RuntimeError(f"contract check failed: {PUBLIC_COMMAND} {' '.join(args)} exit={code}\n{output}")
        if result_kind:
            payload = extract_json_payload(output)
            if not payload or payload.get("ok") is not True or payload.get("result_kind") != result_kind:
                raise RuntimeError(f"contract check returned wrong payload for {' '.join(args)}:\n{output}")
    negative_checks = [
        (["vfs", "tree", "--path", "/", "--format", "tsv"], {"projection_not_supported"}),
        (["call", "rd.session.get_context", "--format", "tsv"], {"tabular_projection_missing"}),
        (["--daemon-context", "package-empty", "diff", "pipeline", "--event-a", "1", "--event-b", "2"], {"session_required"}),
        (["call", "rd.resource.rename", "--format", "json"], {"not_found", "operation_not_found", "tool_not_found", "unknown_operation", "unsupported_operation", "unsupported_command"}),
        (["call", "rd.shader.save_binary", "--format", "json"], {"not_found", "operation_not_found", "tool_not_found", "unknown_operation", "unsupported_operation", "unsupported_command"}),
    ]
    for args, expected_codes in negative_checks:
        _expect_public_error(root, args, expected_codes)


def _verify_no_pre_ga_payload(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as archive:
        for name in archive.namelist():
            normalized = name.replace("\\", "/")
            for marker in PRE_GA_PATH_MARKERS:
                if marker in normalized:
                    raise RuntimeError(f"package contains pre-GA path: {normalized}")
            suffix = Path(normalized).suffix.lower()
            if suffix not in TEXT_SUFFIXES:
                continue
            info = archive.getinfo(name)
            if info.file_size > 5 * 1024 * 1024:
                continue
            text = archive.read(name).decode("utf-8", errors="ignore")
            for marker in PRE_GA_TEXT_MARKERS:
                if marker in text:
                    raise RuntimeError(f"package contains pre-GA marker {marker!r} in {normalized}")


def _verify_no_public_mcp_surface(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as archive:
        for name in archive.namelist():
            normalized = name.replace("\\", "/")
            rel = normalized.removeprefix("rdx-tools/")
            if rel == "mcp" or rel.startswith("mcp/"):
                raise RuntimeError(f"package exposes MCP path: {normalized}")
            if not (rel == "README.md" or rel.startswith("docs/")):
                continue
            if Path(rel).suffix.lower() not in {".md", ".txt"}:
                continue
            info = archive.getinfo(name)
            if info.file_size > 1024 * 1024:
                continue
            text = archive.read(name).decode("utf-8", errors="ignore").lower()
            for marker in MCP_DOC_MARKERS:
                if marker in text:
                    raise RuntimeError(f"package user docs expose MCP public entrypoint marker {marker!r} in {normalized}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an rdx-tools release package")
    parser.add_argument("--zip", dest="zip_path", required=True, help="Release zip path")
    args = parser.parse_args(argv)

    zip_path = Path(args.zip_path).resolve()
    if not zip_path.is_file():
        print(f"[verify] missing package: {zip_path}")
        return 2

    temp_dir = Path(tempfile.mkdtemp(prefix="rdx package verify "))
    try:
        _verify_no_pre_ga_payload(zip_path)
        _verify_no_public_mcp_surface(zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_dir)
        root = _find_package_root(temp_dir)
        _verify_release_manifest(root)
        _verify_license_inventory(root)
        _verify_doctor(root)
        _verify_physical_launcher_file(root)
        _verify_version_payload(root)
        _verify_tools_catalog(root)
        _verify_cli_contract(root)
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] {exc}")
        return 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"[verify] PASS: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
