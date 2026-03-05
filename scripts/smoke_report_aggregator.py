#!/usr/bin/env python3
"""Aggregate command smoke and tool-contract outputs into a fixed desktop report."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _tools_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_output() -> Path:
    return Path.home() / "Desktop" / "rdx_smoke_issues_blockers.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _summary_stats(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(items),
        "pass": sum(1 for item in items if item.get("status") == "pass"),
        "issue": sum(1 for item in items if item.get("status") == "issue"),
        "blocker": sum(1 for item in items if item.get("status") == "blocker"),
    }


def _short(value: Any, max_len: int = 380) -> str:
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _load_command_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = payload.get("results")
    return results if isinstance(results, list) else []


def _load_tool_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    transports = payload.get("transports", {})
    if not isinstance(transports, dict):
        return out

    for transport, transport_payload in transports.items():
        if not isinstance(transport_payload, dict):
            continue
        for item in transport_payload.get("items", []):
            if not isinstance(item, dict):
                continue
            merged = dict(item)
            merged["transport"] = str(merged.get("transport") or transport)
            out.append(merged)

    return out


def _is_remote_app_issue(item: dict[str, Any]) -> bool:
    return str(item.get("issue_type") or "").strip() == "remote_app_dependency"


def _is_sample_compatibility_issue(item: dict[str, Any]) -> bool:
    return str(item.get("issue_type") or "").strip() == "sample_compatibility"


def _is_usability_issue(item: dict[str, Any]) -> bool:
    if str(item.get("tool") or "") != "rdx.bat":
        return False
    return str(item.get("issue_type") or "").strip() in {"usability", "issue"}


def _collect_blockers(items: list[dict[str, Any]]) -> list[tuple[int, str, dict[str, Any]]]:
    blockers: list[tuple[int, str, dict[str, Any]]] = []
    for item in items:
        if str(item.get("status") or "") != "blocker":
            continue

        impact = str(item.get("impact_scope") or item.get("matrix") or "")
        low_impact = impact.lower()
        if "main chain" in low_impact or "contract" in low_impact or "schema" in low_impact:
            severity = 0
        elif "remote_id" in low_impact or "remote" in low_impact:
            severity = 1
        elif "daemon" in low_impact:
            severity = 2
        elif "command" in low_impact or "menu" in low_impact:
            severity = 3
        else:
            severity = 4

        source = str(item.get("tool") or item.get("id") or "")
        blockers.append((severity, source.lower(), item))

    blockers.sort(key=lambda item: (item[0], item[1]))
    return blockers


def _build_tool_diff(tool_items: list[dict[str, Any]]) -> list[str]:
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in tool_items:
        tool_name = str(item.get("tool") or "")
        transport = str(item.get("transport") or "")
        if not tool_name or transport not in {"mcp", "daemon"}:
            continue
        grouped[tool_name][transport] = item

    lines: list[str] = []
    for tool_name, transport_map in sorted(grouped.items()):
        mcp = transport_map.get("mcp", {})
        daemon = transport_map.get("daemon", {})
        mcp_status = mcp.get("status")
        daemon_status = daemon.get("status")
        if not mcp_status or not daemon_status or mcp_status == daemon_status:
            continue

        mcp_issue = _short(str(mcp.get("issue_type") or mcp.get("error_code") or "n/a"), 200)
        daemon_issue = _short(str(daemon.get("issue_type") or daemon.get("error_code") or "n/a"), 200)
        mcp_reason = str(mcp.get("reason") or "")
        daemon_reason = str(daemon.get("reason") or "")

        if mcp_status == "blocker" and daemon_status != "blocker":
            suggestion = "先修 MCP/标准链路；确认 stdio 注册、工具列表与 schema 完整。"
        elif daemon_status == "blocker" and mcp_status != "blocker":
            suggestion = "先修 daemon 路径（daemon 启动、上下文、call 命令顺序、连接保持）。"
        else:
            suggestion = "对比差异原因，两链路分别补齐；优先定位首个报错工具。"

        lines.append(
            f"- `{tool_name}`: mcp={mcp_status}({mcp_issue}) / daemon={daemon_status}({daemon_issue})"
            f"\n  - reason_delta: {_short(mcp_reason or daemon_reason, 220)}"
            f"\n  - suggest: {suggestion}"
        )

    return lines


def _collect_cleanup_residual(command_payload: dict[str, Any], tool_payload: dict[str, Any]) -> list[str]:
    residual: list[str] = []

    command_cleanup = command_payload.get("cleanup", {})
    if isinstance(command_cleanup, dict):
        for key, value in command_cleanup.items():
            residual.append(f"command::{key}={_short(value, 180)}")

    daemon_payload = tool_payload.get("transports", {}).get("daemon", {})
    if isinstance(daemon_payload, dict):
        daemon_cleanup = daemon_payload.get("cleanup", {})
        if isinstance(daemon_cleanup, dict):
            for key, value in daemon_cleanup.items():
                residual.append(f"daemon::{key}={_short(value, 180)}")

    if not residual:
        residual.append("no residual metadata found")

    return residual


def _remote_workflow_summary(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    transports_payload = payload.get("transports", {})
    for transport in ("mcp", "daemon"):
        t_payload = transports_payload.get(transport)
        if not isinstance(t_payload, dict):
            continue
        events = t_payload.get("remote_workflow_events", [])
        if not isinstance(events, list):
            lines.append(f"{transport}: no remote workflow data")
            continue
        if not events:
            lines.append(f"{transport}: no remote workflow events")
            continue
        joined = " -> ".join(str(item) for item in events)
        has_connect = any(str(item).startswith(f"{transport}-connect") for item in events)
        has_tool = any(str(item).startswith(f"{transport}-tool") for item in events)
        has_disconnect = any(str(item).startswith(f"{transport}-disconnect") for item in events)
        if has_connect and has_tool and has_disconnect:
            status = "OK"
        elif has_connect and has_tool:
            status = "INCOMPLETE (missing disconnect)"
        else:
            status = "BROKEN"
        lines.append(f"{transport}: {status} | {joined}")
    if not lines:
        lines.append("no transport payload found")
    return lines


def _collect_clear_state(residual: list[str]) -> tuple[str, list[str]]:
    blockers = [line for line in residual if "False" in line or "failed" in line.lower() or "error" in line.lower()]
    if blockers:
        return "未完全清理（含残留项与手动清理命令）", residual
    return "已清理", residual


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate rdx.bat smoke and tool-contract outputs.")
    parser.add_argument("--command-json", default="intermediate/logs/rdx_bat_command_smoke.json")
    parser.add_argument("--tool-json", default="intermediate/logs/tool_contract_report.json")
    parser.add_argument("--usability-json", default="intermediate/logs/rdx_bat_usability_report.json")
    parser.add_argument("--out", default=str(_default_output()))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = _tools_root()
    command_json = (root / args.command_json).resolve()
    tool_json = (root / args.tool_json).resolve()
    usability_json = (root / args.usability_json).resolve()
    out_path = Path(args.out).resolve()

    command_payload = _load_json(command_json)
    tool_payload = _load_json(tool_json)
    usability_payload = _load_json(usability_json)

    if not command_payload:
        print(f"[aggregate] missing command json: {command_json}")
        return 2
    if not tool_payload:
        print(f"[aggregate] missing tool json: {tool_json}")
        return 2

    command_items = _load_command_items(command_payload)
    tool_items = _load_tool_items(tool_payload)

    command_summary = _summary_stats(command_items)
    tool_summary_mcp = _summary_stats([item for item in tool_items if str(item.get("transport") or "") == "mcp"])
    tool_summary_daemon = _summary_stats([item for item in tool_items if str(item.get("transport") or "") == "daemon"])
    combined = command_items + tool_items
    total_summary = _summary_stats(combined)

    usability = usability_payload.get("usability", {})
    usability_status = str(usability.get("overall") or "UNKNOWN").upper()
    usability_summary = usability.get("summary", {})
    usability_checks = usability.get("checks", [])

    blockers = _collect_blockers(combined)
    issues = [item for item in combined if item.get("status") == "issue"]
    remote_app_issues = [item for item in issues if _is_remote_app_issue(item)]
    sample_compatibility_issues = [item for item in issues if _is_sample_compatibility_issue(item)]
    usability_issues = [item for item in issues if _is_usability_issue(item)]
    other_issues = [
        item
        for item in issues
        if item not in remote_app_issues and item not in sample_compatibility_issues and item not in usability_issues
    ]

    diff_lines = _build_tool_diff(tool_items)
    residual = _collect_cleanup_residual(command_payload, tool_payload)
    cleanup_status, cleanup_details = _collect_clear_state(residual)

    lines: list[str] = [
        "# rdx-tools smoke report (dual sample + rdx.bat usability)",
        "",
        f"- generated_at_utc: {_now_iso()}",
        f"- local_rdc: `{command_payload.get('local_rdc', tool_payload.get('local_rdc', ''))}`",
        f"- remote_rdc: `{command_payload.get('remote_rdc', tool_payload.get('remote_rdc', ''))}`",
        f"- command_json: `{command_json}`",
        f"- tool_json: `{tool_json}`",
        f"- usability_json: `{usability_json}`",
        "",
        "## 总览",
        f"- total: {total_summary['total']}",
        f"- pass: {total_summary['pass']}",
        f"- issue: {total_summary['issue']}",
        f"- blocker: {total_summary['blocker']}",
        "",
        "## 覆盖统计",
        f"- commands: {command_summary['total']} (pass={command_summary['pass']}, issue={command_summary['issue']}, blocker={command_summary['blocker']})",
        f"- tools_mcp: {tool_summary_mcp['total']} (pass={tool_summary_mcp['pass']}, issue={tool_summary_mcp['issue']}, blocker={tool_summary_mcp['blocker']})",
        f"- tools_daemon: {tool_summary_daemon['total']} (pass={tool_summary_daemon['pass']}, issue={tool_summary_daemon['issue']}, blocker={tool_summary_daemon['blocker']})",
        f"- rdx.bat-usability: {usability_summary.get('total', len(usability_checks))} (overall={usability_status})",
        "",
        "## rdx.bat 可直接点开使用",
        f"- status: {usability_status}",
        f"- based_on: {_short((usability.get('impact') or []), 220) or 'usability evidence available in checks section'}",
        "",
        "## Blocker Top（按严重性）",
    ]

    if not blockers:
        lines.append("- (none)")
    else:
        for _, _, item in blockers[:120]:
            lines.extend(
                [
                    f"- tool: {item.get('tool') or item.get('id') or 'unknown'} [{str(item.get('transport') or '')}]",
                    f"  - root_cause: {_short(item.get('reason'), 280)}",
                    f"  - repro_command: `{_short(item.get('repro_command'), 340)}`",
                    f"  - evidence: {_short(item.get('evidence'), 340)}",
                    f"  - impact_scope: {_short(item.get('impact_scope') or item.get('matrix') or 'main chain', 140)}",
                    f"  - fix_hint: {_short(item.get('fix_hint') or 'no direct hint')}",
                ]
            )

    lines.extend(
        [
            "",
            "## Issues",
            "",
            "### remote/app 依赖不足",
        ]
    )
    if not remote_app_issues:
        lines.append("- (none)")
    else:
        for item in remote_app_issues:
            lines.extend(
                [
                    f"- {item.get('tool')} [{item.get('transport')}]",
                    f"  - reason: {_short(item.get('reason'), 240)}",
                    f"  - repro_command: `{_short(item.get('repro_command'), 320)}`",
                    f"  - fix_hint: {_short(item.get('fix_hint'))}",
                ]
            )

    lines.append("### 文案/交互直观性问题")
    if not usability_issues:
        lines.append("- (none)")
    else:
        for item in usability_issues:
            lines.extend(
                [
                    f"- {item.get('tool')} [{item.get('transport', 'command')}]",
                    f"  - reason: {_short(item.get('reason'), 240)}",
                    f"  - repro_command: `{_short(item.get('repro_command'), 320)}`",
                    f"  - fix_hint: {_short(item.get('fix_hint'))}",
                ]
            )

    lines.append("### sample_compatibility 与 remote-toolchain 分层")
    if not sample_compatibility_issues:
        lines.append("- (none)")
    else:
        for item in sample_compatibility_issues:
            lines.extend(
                [
                    f"- {item.get('tool')} [{item.get('transport', 'command')}]",
                    f"  - reason: {_short(item.get('reason'), 240)}",
                    f"  - repro_command: `{_short(item.get('repro_command'), 320)}`",
                    f"  - fix_hint: {_short(item.get('fix_hint'))}",
                    f"  - impact_scope: {_short(item.get('impact_scope') or 'remote matrix path', 140)}",
                ]
            )

    lines.append("### 其他 Issues")
    if not other_issues:
        lines.append("- (none)")
    else:
        for item in other_issues:
            lines.append(
                f"- {item.get('tool')}[{item.get('transport')}] {item.get('error_code')} - {_short(item.get('reason'), 220)}"
            )

    lines.extend(
        [
            "",
            "## MCP / Daemon 差异清单",
        ]
    )
    if not diff_lines:
        lines.append("- (none)")
    else:
        lines.extend(diff_lines)

    lines.extend(
        [
            "",
            "## 清理结果",
            f"- {cleanup_status}",
        ]
    )
    for detail in cleanup_details:
        lines.append(f"- {detail}")

    lines.extend(["", "## remote workflow evidence"])
    lines.extend([f"- {line}" for line in _remote_workflow_summary(tool_payload)])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[aggregate] wrote: {out_path}")

    return 0 if total_summary["blocker"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
