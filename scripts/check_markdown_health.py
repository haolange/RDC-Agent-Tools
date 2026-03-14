from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from scripts._shared import tools_root


RISKY_PATTERNS = (
    "\u93c4",
    "\u935a",
    "\u951b",
    "\u9286",
    "\u9225",
    "\ufffd",
)
SCAN_SKIP_PREFIXES = (
    ".git/",
    "intermediate/",
    ".pytest_cache/",
    ".venv/",
)
REQUIRED_DOCS = (
    "README.md",
    "docs/README.md",
    "docs/quickstart.md",
    "docs/session-model.md",
    "docs/agent-model.md",
    "docs/doc-governance.md",
    "docs/configuration.md",
    "docs/troubleshooting.md",
    "docs/tools.md",
    "docs/android-remote-cli-smoke-prompt.md",
    "scripts/README.md",
)
REQUIRED_NAV_LINKS = {
    "README.md": (
        "docs/session-model.md",
        "docs/agent-model.md",
        "docs/doc-governance.md",
        "docs/tools.md",
        "scripts/README.md",
    ),
    "docs/README.md": (
        "session-model.md",
        "agent-model.md",
        "doc-governance.md",
        "tools.md",
        "../scripts/README.md",
    ),
    "docs/tools.md": (
        "session-model.md",
        "agent-model.md",
    ),
    "docs/doc-governance.md": (
        "../AGENTS.md",
    ),
    "docs/android-remote-cli-smoke-prompt.md": (
        "../README.md",
        "session-model.md",
        "agent-model.md",
        "troubleshooting.md",
        "doc-governance.md",
        "../scripts/README.md",
    ),
}
LOCAL_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
PLACEHOLDER_RE = re.compile(r"\?{4,}")
TOOL_COUNT_RE = re.compile(r"(\d+)\s*(?:[\u4e2a]\s*)?`rd\.\*`\s*tools")
CONTRACT_COUNT_RE = re.compile(r"196\s+tools contract")


def tools_root_path() -> Path:
    return tools_root(__file__)


def iter_markdown_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.md"):
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(prefix) for prefix in SCAN_SKIP_PREFIXES):
            continue
        files.append(path)
    return sorted(files)


def has_utf8_bom(data: bytes) -> bool:
    return data.startswith(b"\xef\xbb\xbf")


def scan_file(root: Path, path: Path) -> tuple[list[str], set[str]]:
    issues: list[str] = []
    seen_links: set[str] = set()
    rel = path.relative_to(root).as_posix()
    data = path.read_bytes()
    if not has_utf8_bom(data):
        issues.append(f"{rel}: missing UTF-8 BOM")

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        issues.append(f"{rel}: invalid UTF-8 ({exc})")
        return issues, seen_links

    root_resolved = root.resolve()
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern in RISKY_PATTERNS:
            if pattern in line:
                issues.append(f"{rel}:{lineno}: suspicious mojibake fragment `{pattern}`")
                break
        if PLACEHOLDER_RE.search(line):
            issues.append(f"{rel}:{lineno}: suspicious placeholder text with repeated `?`")
        for match in LOCAL_LINK_RE.finditer(line):
            target = match.group(1).strip()
            if (not target) or "://" in target or target.startswith("#") or target.startswith("mailto:"):
                continue
            base_target = target.split("#", 1)[0].strip()
            if not base_target:
                continue
            seen_links.add(base_target.replace("\\", "/"))
            resolved = (path.parent / base_target).resolve()
            try:
                resolved.relative_to(root_resolved)
            except ValueError:
                issues.append(f"{rel}:{lineno}: link escapes tools root `{target}`")
                continue
            if not resolved.exists():
                issues.append(f"{rel}:{lineno}: broken local link `{target}`")
    return issues, seen_links


def _load_catalog(root: Path) -> dict:
    path = root / "spec" / "tool_catalog.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8-sig")


def _check_count_consistency(root: Path, issues: list[str], tool_count: int) -> None:
    for rel in ("README.md", "docs/tools.md"):
        text = _read_text(root, rel)
        for match in TOOL_COUNT_RE.finditer(text):
            if int(match.group(1)) != tool_count:
                issues.append(f"{rel}: documented tool count `{match.group(1)}` does not match catalog tool_count `{tool_count}`")
        if CONTRACT_COUNT_RE.search(text):
            issues.append(f"{rel}: outdated fixed-count wording `196 tools contract`")
        if "??????? 196" in text or "?? 196 ?" in text:
            issues.append(f"{rel}: outdated fixed-count wording still present")


def _check_session_tool_mentions(root: Path, issues: list[str], tool_names: set[str]) -> None:
    if not {"rd.session.get_context", "rd.session.update_context"}.issubset(tool_names):
        return
    for rel in ("README.md", "docs/quickstart.md", "docs/session-model.md", "docs/agent-model.md", "docs/troubleshooting.md"):
        text = _read_text(root, rel)
        if "rd.session.get_context" not in text:
            issues.append(f"{rel}: missing required mention `rd.session.get_context`")
    for rel in ("docs/session-model.md", "docs/agent-model.md", "docs/quickstart.md"):
        text = _read_text(root, rel)
        if "rd.session.update_context" not in text:
            issues.append(f"{rel}: missing required mention `rd.session.update_context`")


def _check_remote_consumed_semantics(root: Path, issues: list[str]) -> None:
    for rel in ("README.md", "docs/session-model.md", "docs/agent-model.md", "docs/troubleshooting.md"):
        text = _read_text(root, rel)
        if "remote_handle_consumed" not in text:
            issues.append(f"{rel}: missing required remote lifecycle snippet `remote_handle_consumed`")


def _check_agent_self_test_guidance(root: Path, issues: list[str]) -> None:
    text = _read_text(root, "AGENTS.md")
    required = (
        "Conflict policy:",
        "docs/session-model.md",
        "docs/agent-model.md",
        "docs/troubleshooting.md",
        "docs/doc-governance.md",
        "docs/android-remote-cli-smoke-prompt.md",
        "rd.remote.connect",
        "rd.remote.ping",
        "rd.capture.open_replay",
    )
    for snippet in required:
        if snippet not in text:
            issues.append(f"AGENTS.md: missing self-test guidance snippet `{snippet}`")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Markdown encoding health")
    parser.parse_args(argv)

    root = tools_root_path()
    files = iter_markdown_files(root)
    issues: list[str] = []
    link_map: dict[str, set[str]] = {}
    for rel in REQUIRED_DOCS:
        if not (root / rel).is_file():
            issues.append(f"{rel}: required markdown document is missing")
    for path in files:
        file_issues, seen_links = scan_file(root, path)
        issues.extend(file_issues)
        link_map[path.relative_to(root).as_posix()] = seen_links

    for rel, required_links in REQUIRED_NAV_LINKS.items():
        seen = link_map.get(rel, set())
        for target in required_links:
            if target not in seen:
                issues.append(f"{rel}: missing required link `{target}`")

    catalog = _load_catalog(root)
    tool_count = int(catalog.get("tool_count") or len(catalog.get("tools", [])))
    tool_names = {str(item.get("name", "")).strip() for item in catalog.get("tools", [])}

    _check_count_consistency(root, issues, tool_count)
    _check_session_tool_mentions(root, issues, tool_names)
    _check_remote_consumed_semantics(root, issues)
    _check_agent_self_test_guidance(root, issues)

    if issues:
        print("[md] Markdown health check failed")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print(f"[md] Markdown health check passed ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
