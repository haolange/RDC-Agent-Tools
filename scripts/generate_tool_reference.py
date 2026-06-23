"""Generate the reader-facing rd.* tool reference from spec/tool_catalog.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "spec" / "tool_catalog.json"
DEFAULT_OUTPUT = ROOT / "docs" / "tool-reference.md"


def _read_catalog(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    tools = payload.get("tools")
    if not isinstance(tools, list):
        raise ValueError(f"catalog tools field is missing or invalid: {path}")
    return payload


def _cell(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "<br>")
    text = text.replace("|", "\\|")
    return text or "-"


def _group_tools(payload: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    tools = [item for item in payload.get("tools", []) if isinstance(item, dict)]
    groups_payload = payload.get("groups")
    ordered_group_names: list[str] = []
    if isinstance(groups_payload, dict):
        ordered_group_names.extend(str(name) for name in groups_payload.keys())
    for tool in tools:
        group = str(tool.get("group") or "Ungrouped")
        if group not in ordered_group_names:
            ordered_group_names.append(group)
    grouped: list[tuple[str, list[dict[str, Any]]]] = []
    for group in ordered_group_names:
        members = [tool for tool in tools if str(tool.get("group") or "Ungrouped") == group]
        if members:
            grouped.append((group, members))
    return grouped


def generate_tool_reference(catalog_path: Path = DEFAULT_CATALOG) -> str:
    payload = _read_catalog(catalog_path)
    tools = [item for item in payload.get("tools", []) if isinstance(item, dict)]
    grouped = _group_tools(payload)
    declared_count = int(payload.get("tool_count") or len(tools))
    group_count = len(grouped)

    lines: list[str] = [
        "# Tool Reference",
        "",
        "This file is generated from `spec/tool_catalog.json`. Do not edit it by hand; run `python scripts/generate_tool_reference.py`.",
        "",
        f"- Tool count: {declared_count}",
        f"- Group count: {group_count}",
        "- Canonical transport: `rdx call <rd.*> --format json`",
        "- Human facade: `rdx event|pipeline|shader|export|pixel|resource ...` maps to the same canonical tools",
        "- Scope: Windows x64 local replay plus remote Android replay",
        "",
        "## Groups",
        "",
        "| Group | Tools |",
        "| --- | ---: |",
    ]
    for group, members in grouped:
        lines.append(f"| {_cell(group)} | {len(members)} |")

    for group, members in grouped:
        lines.extend([
            "",
            f"## {group}",
            "",
            "| Tool | Summary | Parameters | Prerequisites |",
            "| --- | --- | --- | --- |",
        ])
        for tool in members:
            prereq_items = []
            for prereq in tool.get("prerequisites") or []:
                if isinstance(prereq, dict):
                    requires = str(prereq.get("requires") or "").strip()
                    via = ", ".join(str(item) for item in prereq.get("via_tools") or [])
                    reason = str(prereq.get("reason") or "").strip()
                    prereq_items.append("; ".join(part for part in (requires, via, reason) if part))
            prereqs = "<br>".join(prereq_items)
            lines.append(
                "| "
                + _cell(tool.get("name"))
                + " | "
                + _cell(tool.get("description"))
                + " | "
                + _cell(tool.get("parameter_raw") or ", ".join(str(item) for item in tool.get("param_names") or []))
                + " | "
                + _cell(prereqs)
                + " |"
            )

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate docs/tool-reference.md from spec/tool_catalog.json")
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--check", action="store_true", help="Fail if the committed output is stale")
    args = parser.parse_args(argv)

    catalog_path = Path(args.catalog).resolve()
    out_path = Path(args.out).resolve()
    rendered = generate_tool_reference(catalog_path)
    if args.check:
        current = out_path.read_text(encoding="utf-8-sig") if out_path.is_file() else ""
        if current != rendered:
            print(f"[tool-reference] stale: {out_path}")
            return 1
        print(f"[tool-reference] fresh: {out_path}")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8-sig")
    print(f"[tool-reference] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
