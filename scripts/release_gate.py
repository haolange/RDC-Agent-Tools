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
import zipfile
from dataclasses import dataclass
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from rdx.python_runtime import validate_bundled_python_layout
from scripts import package_release as release_packager
from scripts._shared import run_subprocess, tools_root, write_text
from scripts.generate_tool_reference import generate_tool_reference


REQUIRED_DIRS = [
    "rdx",
    "bin",
    "cli",
    "spec",
    "policy",
    "docs",
    "tests",
    "binaries/windows/x64/python",
    "binaries/windows/x64/pymodules",
    "intermediate/runtime/rdx_cli",
    "intermediate/runtime/worker-state",
    "intermediate/artifacts",
    "intermediate/pytest",
    "intermediate/logs",
]

REQUIRED_FILES = [
    "pyproject.toml",
    "CHANGELOG.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/rdx-native-agent-playbook.md",
    "docs/tool-reference.md",
]

BASH_SMOKE_LOG = "intermediate/logs/smoke_cli.log"
PUBLIC_COMMAND = "rdx"
WINDOWS_LAUNCHER_FILE = "rdx.bat"
EXPECTED_PUBLIC_COMMANDS = [PUBLIC_COMMAND]
EXPECTED_ENTRYPOINTS = [WINDOWS_LAUNCHER_FILE, "bin/rdx", "cli/run_cli.py"]
REMOVED_CATALOG_TOOLS = {"rd.resource.rename", "rd.shader.save_binary"}

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
MCP_DOC_MARKERS = (
    "mcp/run_mcp.py",
    "model context protocol",
    "mcp server",
    "mcp public",
)
USER_DOCS = [
    "README.md",
    "docs/install.md",
    "docs/quickstart.md",
    "docs/agent-integration.md",
    "docs/configuration.md",
    "docs/troubleshooting.md",
    "docs/public-contract.md",
    "docs/stability.md",
    "docs/release-notes.md",
]
USER_PATH_FORBIDDEN_RULES = (
    re.compile(r"\buv\.lock\b", re.IGNORECASE),
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


def _launcher_env(root: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("RDX_PYTHON", None)
    if root is not None:
        env["PATH"] = str(root) + os.pathsep + str(env.get("PATH") or "")
        pathext = str(env.get("PATHEXT") or "")
        if ".BAT" not in pathext.upper().split(";"):
            env["PATHEXT"] = pathext + (";" if pathext else "") + ".BAT"
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


def _run_public_command(args: list[str], cwd: Path) -> tuple[bool, str]:
    return _run([_cmd_exe(), "/c", PUBLIC_COMMAND, *args], cwd, env=_launcher_env(cwd))


def _run_public_command_expect_error(args: list[str], cwd: Path, *, expected_codes: set[str]) -> tuple[bool, str]:
    code, out, err = run_subprocess([_cmd_exe(), "/c", PUBLIC_COMMAND, *args], cwd=cwd, env=_launcher_env(cwd))
    detail = ((out or "") + (err or "")).strip()
    try:
        from scripts._shared import extract_json_payload

        payload = extract_json_payload(detail)
    except Exception:
        payload = {}
    actual_code = str(((payload or {}).get("error") or {}).get("code") or "")
    if code != 0 and actual_code in expected_codes:
        return True, detail
    expected = ", ".join(sorted(expected_codes))
    return False, f"expected non-zero one of `{expected}`, got exit={code} code={actual_code}\n{detail}"


def _run_windows_launcher_file(args: list[str], cwd: Path) -> tuple[bool, str]:
    return _run([_cmd_exe(), "/c", WINDOWS_LAUNCHER_FILE, *args], cwd, env=_launcher_env())


def _bundled_python_for_gate(root: Path) -> str:
    bundled = root / "binaries" / "windows" / "x64" / "python" / "python.exe"
    return str(bundled) if bundled.is_file() else sys.executable


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


def _check_user_docs_no_bat_command_examples(root: Path) -> tuple[bool, str]:
    command_pattern = re.compile(r"(?i)(?:^|\s)(?:\.\\)?rdx\.bat\s+\S")
    for rel in USER_DOCS:
        path = root / rel
        if not path.is_file():
            return False, f"missing user doc: {path}"
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if command_pattern.search(line):
                return False, f"{rel}:{lineno}: use `{PUBLIC_COMMAND}` for user commands: {line.strip()}"
    return True, "user docs reserve rdx.bat for launcher-file references only"


def _check_help_uses_public_command(help_text: str) -> tuple[bool, str]:
    if not help_text.strip():
        return False, "help output is empty"
    if re.search(r"(?i)(?:^|\s)(?:\.\\)?rdx\.bat\s+\S", help_text):
        return False, "help output contains rdx.bat command examples"
    if "usage: rdx" not in help_text:
        return False, "help output does not advertise usage: rdx"
    return True, "help output advertises rdx and no rdx.bat command examples"


def _check_catalog_public_surface(root: Path) -> tuple[bool, str]:
    catalog_path = root / "spec" / "tool_catalog.json"
    if not catalog_path.is_file():
        return False, f"missing catalog: {catalog_path}"
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"invalid catalog json: {exc}"
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False, "catalog tools field is missing or invalid"
    names = {str(item.get("name") or "").strip() for item in tools if isinstance(item, dict)}
    removed = sorted(REMOVED_CATALOG_TOOLS & names)
    if removed:
        return False, f"removed aliases still in active catalog: {removed}"
    declared_count = int(payload.get("tool_count") or len(tools))
    if declared_count != len(tools):
        return False, f"catalog tool_count mismatch: declared={declared_count} actual={len(tools)}"
    if len(tools) != 194:
        return False, f"expected 194 active tools after alias convergence, got {len(tools)}"
    catalog_text = json.dumps(payload, ensure_ascii=False)
    if "\u517c\u5bb9\u5de5\u5177" in catalog_text:
        return False, "active catalog contains removed alias-tool wording"
    return True, "active catalog has 194 tools and no removed aliases"


def _check_tool_reference_fresh(root: Path) -> tuple[bool, str]:
    catalog_path = root / "spec" / "tool_catalog.json"
    doc_path = root / "docs" / "tool-reference.md"
    if not catalog_path.is_file():
        return False, f"missing catalog: {catalog_path}"
    if not doc_path.is_file():
        return False, f"missing tool reference: {doc_path}"
    try:
        expected = generate_tool_reference(catalog_path)
        current = doc_path.read_text(encoding="utf-8-sig")
    except Exception as exc:
        return False, f"tool reference freshness check failed: {exc}"
    if current != expected:
        return False, "docs/tool-reference.md is stale; run python scripts/generate_tool_reference.py"
    return True, "docs/tool-reference.md matches spec/tool_catalog.json"


def _check_no_mcp_public_surface(root: Path) -> tuple[bool, str]:
    mcp_root = root / "mcp"
    if mcp_root.exists():
        return False, "mcp/ must not be part of the active release source surface"
    release_paths = {str(item["path"]) for item in _release_source_manifest(root)}
    leaked = sorted(path for path in release_paths if path == "mcp" or path.startswith("mcp/"))
    if leaked:
        return False, f"release source manifest exposes MCP paths: {leaked[:5]}"
    for rel in USER_DOCS:
        path = root / rel
        if not path.is_file():
            return False, f"missing user doc: {path}"
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for marker in MCP_DOC_MARKERS:
            if marker in text:
                return False, f"{rel}: exposes MCP public entrypoint marker `{marker}`"
    return True, "release source and user docs expose no MCP public entrypoint"


def _check_reports(root: Path, *, require_smoke_reports: bool) -> tuple[bool, str]:
    log_path = root / BASH_SMOKE_LOG
    if log_path.is_file():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        if "[smoke] PASS" in text:
            return True, "bash CLI smoke log is present and passed"
        if require_smoke_reports:
            return False, f"bash CLI smoke log did not contain [smoke] PASS: {BASH_SMOKE_LOG}"
        return True, "bash CLI smoke log present but not marked passed; smoke evidence is optional unless required"

    if require_smoke_reports:
        return False, f"missing bash CLI smoke log: {BASH_SMOKE_LOG}"

    return True, "bash CLI smoke optional; run bash scripts/smoke_cli.sh before requiring smoke reports"


def _find_release_package(root: Path, raw_package: str) -> Path | None:
    if raw_package:
        candidate = Path(raw_package)
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.resolve()
    dist = root / "dist"
    if not dist.is_dir():
        return None
    packages = sorted(dist.glob("rdx-tools-*-windows-x64.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return packages[0].resolve() if packages else None


def _check_release_package(root: Path, *, raw_package: str, required: bool) -> tuple[bool, str]:
    package_path = _find_release_package(root, raw_package)
    if package_path is None:
        if required:
            return False, "missing release package under dist/rdx-tools-*-windows-x64.zip"
        return True, "release package check skipped; pass --require-release-package for GA"
    if not package_path.is_file():
        return False, f"release package not found: {package_path}"
    checksums = package_path.parent / "SHA256SUMS"
    if not checksums.is_file():
        return False, f"missing SHA256SUMS next to package: {checksums}"
    checksum_text = checksums.read_text(encoding="utf-8", errors="replace")
    if package_path.name not in checksum_text:
        return False, f"SHA256SUMS does not list {package_path.name}"
    verify_cmd = [
        sys.executable,
        "scripts/verify_release_package.py",
        "--zip",
        str(package_path),
    ]
    ok, detail = _run(verify_cmd, cwd=root)
    if not ok:
        return False, detail
    ok_match, match_detail = _check_package_matches_source(root, package_path)
    if not ok_match:
        return False, match_detail
    return True, f"verified release package: {package_path.name}; {match_detail}"


def _release_source_manifest(root: Path) -> list[dict[str, object]]:
    allowed_roots = release_packager.RELEASE_ROOT_FILES | release_packager.RELEASE_DIRS
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() or release_packager._should_skip(path, root):  # noqa: SLF001
            continue
        rel = path.relative_to(root)
        if not rel.parts or rel.parts[0] not in allowed_roots:
            continue
        entries.append(
            {
                "path": rel.as_posix(),
                "size": int(path.stat().st_size),
                "sha256": _sha256(path),
            }
        )
    return entries


def _check_package_matches_source(root: Path, package_path: Path) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(package_path, "r") as archive:
            payload = json.loads(archive.read("rdx-tools/RELEASE_MANIFEST.json").decode("utf-8"))
    except Exception as exc:
        return False, f"release package manifest unreadable: {exc}"

    files = payload.get("files")
    if not isinstance(files, list):
        return False, "release package manifest files field is missing or invalid"

    expected = {str(item["path"]): item for item in _release_source_manifest(root)}
    actual: dict[str, dict[str, object]] = {}
    for item in files:
        if not isinstance(item, dict):
            return False, "release package manifest contains a non-object file entry"
        rel = str(item.get("path") or "")
        if not rel:
            return False, "release package manifest contains an empty file path"
        actual[rel] = item

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        return False, (
            "release package is stale relative to source tree; "
            f"missing={missing[:5]} extra={extra[:5]}"
        )

    for rel, exp in expected.items():
        got = actual[rel]
        got_size = got.get("size")
        if got_size is None or int(got_size) != int(exp["size"]) or str(got.get("sha256") or "").lower() != str(exp["sha256"]):
            return False, f"release package is stale relative to source tree: {rel}"

    return True, f"source manifest matched {len(expected)} files"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run release gate checks")
    parser.add_argument("--report", default="intermediate/logs/release_gate_report.md")
    parser.add_argument(
        "--require-smoke-reports",
        action="store_true",
        help="Fail unless the bash CLI smoke log is present and marked passed.",
    )
    parser.add_argument(
        "--require-release-package",
        action="store_true",
        help="Fail unless a verified rdx-tools Windows x64 release package exists.",
    )
    parser.add_argument(
        "--release-package",
        default="",
        help="Explicit release zip path to verify.",
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
    ok_docs_command, docs_command_detail = _check_user_docs_no_bat_command_examples(root)
    results.append(("docs:public-command-examples", ok_docs_command, docs_command_detail))
    ok_catalog_surface, catalog_surface_detail = _check_catalog_public_surface(root)
    results.append(("catalog:public-surface", ok_catalog_surface, catalog_surface_detail))
    ok_tool_ref, tool_ref_detail = _check_tool_reference_fresh(root)
    results.append(("docs:tool-reference-fresh", ok_tool_ref, tool_ref_detail))
    ok_mcp_surface, mcp_surface_detail = _check_no_mcp_public_surface(root)
    results.append(("mcp:no-public-entrypoint", ok_mcp_surface, mcp_surface_detail))

    ok_manifest, msg_manifest = _check_manifest(root)
    results.append(("manifest:integrity", ok_manifest, msg_manifest))
    ok_bundled_python, bundled_python_detail = _check_bundled_python()
    results.append(("manifest:bundled-python", ok_bundled_python, bundled_python_detail))

    ok_public_help, public_help = _run_public_command(["--help"], cwd=root)
    results.append(("entry:rdx --help", ok_public_help, public_help))
    ok_help_contract, help_contract = _check_help_uses_public_command(public_help)
    results.append(("help:public-command", ok_public_help and ok_help_contract, help_contract))
    ok_public_doctor, public_doctor = _run_public_command(["--json", "doctor"], cwd=root)
    results.append(("entry:rdx --json doctor", ok_public_doctor, public_doctor))
    ok_public_version, public_version = _run_public_command(["--version"], cwd=root)
    results.append(("entry:rdx --version", ok_public_version, public_version))
    ok_public_version_json, public_version_json = _run_public_command(["version", "--json"], cwd=root)
    results.append(("entry:rdx version --json", ok_public_version_json, public_version_json))
    ok_public_tools, public_tools = _run_public_command(["tools", "list", "--json"], cwd=root)
    results.append(("entry:rdx tools list --json", ok_public_tools, public_tools))
    ok_context_status, context_status = _run_public_command(["context", "status", "--json"], cwd=root)
    results.append(("entry:rdx context status --json", ok_context_status, context_status))
    ok_context_list, context_list = _run_public_command(["context", "list", "--json"], cwd=root)
    results.append(("entry:rdx context list --json", ok_context_list, context_list))
    ok_context_update, context_update = _run_public_command(
        ["--daemon-context", "release-gate-context", "context", "update", "--key", "notes", "--value", "release-gate", "--json"],
        cwd=root,
    )
    results.append(("entry:rdx context update --json", ok_context_update, context_update))
    ok_context_clear, context_clear = _run_public_command(
        ["--daemon-context", "release-gate-context", "context", "clear", "--json"],
        cwd=root,
    )
    results.append(("entry:rdx context clear --json", ok_context_clear, context_clear))
    ok_vfs_tsv, vfs_tsv = _run_public_command(["vfs", "ls", "--path", "/", "--format", "tsv"], cwd=root)
    results.append(("entry:rdx vfs ls --format tsv", ok_vfs_tsv, vfs_tsv))
    ok_physical_launcher, physical_launcher = _run_windows_launcher_file(["--non-interactive", "--json", "doctor"], cwd=root)
    results.append(("launcher-file:rdx.bat --non-interactive --json doctor", ok_physical_launcher, physical_launcher))
    ok_vfs_bad_tsv, vfs_bad_tsv = _run_public_command_expect_error(
        ["vfs", "tree", "--path", "/", "--format", "tsv"],
        cwd=root,
        expected_codes={"projection_not_supported"},
    )
    results.append(("negative:vfs tree tsv projection", ok_vfs_bad_tsv, vfs_bad_tsv))
    ok_call_bad_tsv, call_bad_tsv = _run_public_command_expect_error(
        ["call", "rd.session.get_context", "--format", "tsv"],
        cwd=root,
        expected_codes={"tabular_projection_missing"},
    )
    results.append(("negative:call context tsv projection", ok_call_bad_tsv, call_bad_tsv))
    unsupported_alias_codes = {"not_found", "operation_not_found", "tool_not_found", "unknown_operation", "unsupported_operation", "unsupported_command"}
    ok_removed_resource_alias, removed_resource_alias = _run_public_command_expect_error(
        ["call", "rd.resource.rename", "--format", "json"],
        cwd=root,
        expected_codes=unsupported_alias_codes,
    )
    results.append(("negative:removed rd.resource.rename", ok_removed_resource_alias, removed_resource_alias))
    ok_removed_shader_alias, removed_shader_alias = _run_public_command_expect_error(
        ["call", "rd.shader.save_binary", "--format", "json"],
        cwd=root,
        expected_codes=unsupported_alias_codes,
    )
    results.append(("negative:removed rd.shader.save_binary", ok_removed_shader_alias, removed_shader_alias))
    ok_diff_no_session, diff_no_session = _run_public_command_expect_error(
        ["--daemon-context", "release-gate-empty", "diff", "pipeline", "--event-a", "1", "--event-b", "2"],
        cwd=root,
        expected_codes={"session_required"},
    )
    results.append(("negative:diff pipeline no session", ok_diff_no_session, diff_no_session))
    ok_assert_no_session, assert_no_session = _run_public_command_expect_error(
        ["--daemon-context", "release-gate-empty", "assert", "pipeline", "--event-a", "1", "--event-b", "2"],
        cwd=root,
        expected_codes={"session_required"},
    )
    results.append(("negative:assert pipeline no session", ok_assert_no_session, assert_no_session))
    ok_cli_help, cli_help = _run([sys.executable, "cli/run_cli.py", "--help"], cwd=root)
    results.append(("entry:dev-python cli/run_cli.py --help", ok_cli_help, cli_help))
    gate_python = _bundled_python_for_gate(root)
    ok_cli_doctor, cli_doctor = _run([gate_python, "cli/run_cli.py", "--json", "doctor"], cwd=root)
    results.append(("entry:bundled-python cli/run_cli.py --json doctor", ok_cli_doctor, cli_doctor))
    ok_catalog, catalog_detail = _run([sys.executable, "spec/validate_catalog.py"], cwd=root)
    results.append(("spec:catalog-validation", ok_catalog, catalog_detail))
    ok_md_health, md_health = _run([sys.executable, "scripts/check_markdown_health.py"], cwd=root)
    results.append(("docs:markdown-health", ok_md_health, md_health))

    ok_reports, report_detail = _check_reports(root, require_smoke_reports=bool(args.require_smoke_reports))
    results.append(("reports:smoke-suite", ok_reports, report_detail))
    ok_package, package_detail = _check_release_package(
        root,
        raw_package=str(args.release_package or ""),
        required=bool(args.require_release_package),
    )
    results.append(("release:package", ok_package, package_detail))

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
