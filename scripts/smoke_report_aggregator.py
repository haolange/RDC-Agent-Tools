#!/usr/bin/env python3
"""Aggregate rdx.bat smoke + tool contract reports into a unified smoke summary."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from scripts._shared import load_json, resolve_repo_path, tools_root, write_text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tools_root() -> Path:
    return tools_root(__file__)


def _safe(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:
            return str(v)
    return str(v)


def _tool_command_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    transports = payload.get("transports", {})
    if not isinstance(transports, dict):
        return out
    for transport_name, transport_payload in transports.items():
        if not isinstance(transport_payload, dict):
            continue
        items = transport_payload.get("items", [])
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            copy = dict(item)
            copy.setdefault("transport", str(transport_name))
            out.append(copy)
    return out


def _is_scope_skip_item(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "")
    if status == "scope_skip":
        return True
    return str(item.get("issue_type") or "") == "scope_skip"


def _overall_status(blocker: int, issue: int) -> str:
    if blocker > 0:
        return "FAIL"
    if issue > 0:
        return "PARTIAL"
    return "PASS"


def _command_summary(payload: dict[str, Any]) -> dict[str, int]:
    results = payload.get("results", [])
    if not isinstance(results, list):
        return {"total": 0, "pass": 0, "issue": 0, "blocker": 0}
    return {
        "total": len(results),
        "pass": sum(1 for item in results if isinstance(item, dict) and item.get("status") == "pass"),
        "issue": sum(1 for item in results if isinstance(item, dict) and item.get("status") == "issue"),
        "blocker": sum(1 for item in results if isinstance(item, dict) and item.get("status") == "blocker"),
    }


def _transport_summary(payload: dict[str, Any], transport: str) -> dict[str, int]:
    items = [item for item in _tool_command_items(payload) if item.get("transport") == transport]
    if items:
        return {
            "total": len(items),
            "pass": sum(1 for item in items if item.get("status") == "pass"),
            "issue": sum(1 for item in items if item.get("status") == "issue" and not _is_scope_skip_item(item)),
            "blocker": sum(1 for item in items if item.get("status") == "blocker"),
            "scope_skip": sum(1 for item in items if _is_scope_skip_item(item)),
            "callable_pass": sum(1 for item in items if bool(item.get("callable"))),
            "contract_pass": sum(1 for item in items if bool(item.get("contract"))),
        }

    transport_payload = payload.get("transports", {}).get(transport, {})
    if not isinstance(transport_payload, dict):
        return {"total": 0, "pass": 0, "issue": 0, "blocker": 0, "scope_skip": 0}
    summary = transport_payload.get("summary", {})
    if isinstance(summary, dict):
        return {
            "total": int(summary.get("total", 0)),
            "pass": int(summary.get("pass", 0)),
            "issue": int(summary.get("issue", 0)),
            "blocker": int(summary.get("blocker", 0)),
            "scope_skip": int(summary.get("scope_skip", 0)),
            "callable_pass": int(summary.get("callable_pass", 0)),
            "contract_pass": int(summary.get("contract_pass", 0)),
        }
    return {"total": 0, "pass": 0, "issue": 0, "blocker": 0, "scope_skip": 0}


def _transport_health_tool_count(payload: dict[str, Any], transport: str) -> dict[str, int]:
    items = [item for item in _tool_command_items(payload) if item.get("transport") == transport]
    if not items:
        return {"local": 0, "remote": 0, "total": 0}

    local_total = sum(1 for item in items if str(item.get("matrix") or "") == "local")
    remote_total = sum(1 for item in items if str(item.get("matrix") or "") == "remote")
    skip = sum(1 for item in items if _is_scope_skip_item(item))
    return {
        "total": len(items),
        "local": local_total,
        "remote": remote_total,
        "scope_skip": skip,
    }


def _collect_scope_skip(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        if not _is_scope_skip_item(item):
            continue
        result.append(item)
    return result


def _collect_items_by_status(items: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    return [item for item in items if str(item.get("status") or "") == status]


def _collect_env_issues(command_payload: dict[str, Any], tool_payload: dict[str, Any]) -> list[dict[str, Any]]:
    env_items: list[dict[str, Any]] = []
    for item in command_payload.get("results", []) if isinstance(command_payload.get("results"), list) else []:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("id") or "") in {"mcp-ensure-env", "cli-daemon-start", "cli-daemon-status", "cli-daemon-stop"}
            and str(item.get("status") or "") != "pass"
        ):
            env_items.append(item)
        if str(item.get("error_code") or "") in {
            "runtime_layout_missing",
            "dependencies_missing",
            "renderdoc_import_failed",
            "no_python_found",
            "startup_failed",
        }:
            env_items.append(item)

    for transport_name in ("mcp", "daemon"):
        transport_payload = tool_payload.get("transports", {}).get(transport_name, {})
        if not isinstance(transport_payload, dict):
            continue
        if isinstance(transport_payload.get("fatal_error"), str) and transport_payload.get("fatal_error"):
            env_items.append(
                {
                    "tool": transport_name,
                    "transport": transport_name,
                    "matrix": "local",
                    "status": "blocker",
                    "reason": str(transport_payload.get("fatal_error")),
                    "issue_type": "env",
                    "impact_scope": "runtime setup",
                    "error_code": "startup_blocked",
                    "fix_hint": "Verify runtime directories and RenderDoc bootstrap state.",
                    "repro_command": f"python {transport_name}/run_{transport_name}.py --help",
                    "evidence": str(transport_payload.get("fatal_error")),
                }
            )

    return env_items


def _cleanliness(payload: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    blocked = False

    command_cleanup = payload.get("command", {}).get("cleanup") if isinstance(payload.get("command"), dict) else payload.get("cleanup")
    if isinstance(command_cleanup, dict):
        daemons = command_cleanup.get("contexts") or []
        if daemons and not all(isinstance(i, str) and i for i in daemons):
            blocked = True
            reasons.append("command cleanup contains non-string context entries")
    else:
        # No explicit cleanup section means command report cannot assert clean state.
        reasons.append("command report missing cleanup metadata")

    tool_payload = payload.get("tool", {})
    for transport in ("mcp", "daemon"):
        transport_payload = tool_payload.get("transports", {}).get(transport, {}) if isinstance(tool_payload, dict) else {}
        cleanup = transport_payload.get("cleanup", {}) if isinstance(transport_payload, dict) else {}
        if isinstance(cleanup, dict):
            for key, value in cleanup.items():
                if isinstance(value, str) and ("fail" in value.lower() or "error" in value.lower()):
                    blocked = True
                    reasons.append(f"{transport} cleanup issue: {value}")

    if not reasons and not blocked:
        return "已清理", []

    if blocked:
        return "未完全清理（含原因）", reasons

    return "已清理", reasons


def _append_blocklist(lines: list[str], items: list[dict[str, Any]], title: str) -> None:
    if title:
        lines.append(f"### {title}")
    if not items:
        lines.append("- (none)")
        return
    for item in items:
        transport = item.get("transport")
        tool = item.get("tool") or item.get("id")
        status = item.get("status")
        scope = item.get("matrix") or item.get("impact_scope") or "unknown"
        reason = item.get("reason") or ""
        code = item.get("error_code") or ""
        repro = item.get("repro_command") or ""
        lines.append(f"- [{transport or 'command'}] {tool} ({scope}) [{status}] {reason}")
        lines.append(f"  - error_code: {code}")
        if repro:
            lines.append(f"  - repro: `{_safe(repro)}`")
        if item.get("fix_hint"):
            lines.append(f"  - fix_hint: {item.get('fix_hint')}")


def _append_stats(lines: list[str], transport_name: str, summary: dict[str, int], counts: dict[str, int]) -> None:
    lines.extend(
        [
            f"#### {transport_name}",
            f"- total: {summary['total']}",
            f"- local: {counts.get('local', 0)}",
            f"- remote: {counts.get('remote', 0)}",
            f"- pass: {summary['pass']}",
            f"- issue: {summary['issue']}",
            f"- blocker: {summary['blocker']}",
            f"- scope_skip: {summary['scope_skip']}",
            f"- local_effective: {max(0, summary['total'] - summary['scope_skip']) if summary['total'] >= summary['scope_skip'] else 0}",
            f"- callable_pass: {summary.get('callable_pass', 0)}",
            f"- contract_pass: {summary.get('contract_pass', 0)}",
        ]
    )


def _write_report(
    out_path: Path,
    command_payload: dict[str, Any],
    tool_payload: dict[str, Any],
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    command_summary = _command_summary(command_payload)
    transport_summaries = {
        name: _transport_summary(tool_payload, name) for name in ("mcp", "daemon")
    }
    tool_items = _tool_command_items(tool_payload)

    mcp_scope_skip = _collect_scope_skip([item for item in tool_items if str(item.get("transport") or "") == "mcp"])
    daemon_scope_skip = _collect_scope_skip([item for item in tool_items if str(item.get("transport") or "") == "daemon"])
    all_blockers = _collect_items_by_status(tool_items, "blocker") + [
        item
        for item in command_payload.get("results", [])
        if isinstance(item, dict) and item.get("status") == "blocker"
    ]
    all_issues = [item for item in tool_items if str(item.get("status") or "") == "issue" and not _is_scope_skip_item(item)] + [
        item
        for item in command_payload.get("results", [])
        if isinstance(item, dict) and item.get("status") == "issue"
    ]

    total_blocker = command_summary["blocker"] + transport_summaries["mcp"]["blocker"] + transport_summaries["daemon"]["blocker"]
    total_issue = command_summary["issue"] + transport_summaries["mcp"]["issue"] + transport_summaries["daemon"]["issue"]

    global_status = _overall_status(blocker=total_blocker, issue=total_issue)
    env_issues = _collect_env_issues(command_payload, tool_payload)

    cleanup_status, cleanup_reasons = _cleanliness({"command": command_payload, "tool": tool_payload})

    mcp_count = _transport_health_tool_count(tool_payload, "mcp")
    daemon_count = _transport_health_tool_count(tool_payload, "daemon")

    lines = [
        "# RDC-Tools 本地链路稳定性汇总",
        "",
        f"- generated_at_utc: {_now_iso()}",
        f"- local_rdc: `{tool_payload.get('local_rdc', command_payload.get('local_rdc', ''))}`",
        f"- remote_rdc: `{tool_payload.get('remote_rdc', command_payload.get('remote_rdc', ''))}`",
        f"- mcp_tools: {transport_summaries['mcp']['total']}",
        f"- daemon_tools: {transport_summaries['daemon']['total']}",
        f"- scope_skip_total: {transport_summaries['mcp']['scope_skip'] + transport_summaries['daemon']['scope_skip']}",
        f"- command_blocker: {command_summary['blocker']}",
        f"- tool_blocker: {transport_summaries['mcp']['blocker'] + transport_summaries['daemon']['blocker']}",
        f"- global_status: {global_status}",
        "",
        "## 一、总体结论",
        f"- 结论: {global_status}",
        f"- 命令层可用性: pass={command_summary['pass']} issue={command_summary['issue']} blocker={command_summary['blocker']}",
        f"- MCP: pass={transport_summaries['mcp']['pass']} issue={transport_summaries['mcp']['issue']} blocker={transport_summaries['mcp']['blocker']} scope_skip={transport_summaries['mcp']['scope_skip']}",
        f"- Daemon: pass={transport_summaries['daemon']['pass']} issue={transport_summaries['daemon']['issue']} blocker={transport_summaries['daemon']['blocker']} scope_skip={transport_summaries['daemon']['scope_skip']}",
        "",
        "## 二、环境与运行问题（daemon/mcp/env）",
        f"- environment_items: {len(env_issues)}",
        "- details:",
    ]

    for item in env_issues:
        tool = item.get("tool")
        code = item.get("error_code") or "unknown"
        reason = item.get("reason") or ""
        lines.append(f"  - {tool}: {code} | {reason}")

    if not env_issues:
        lines.append("  - (none)")

    catalog_target = int(tool_payload.get("catalog_count", 0) or 0)
    lines.extend(
        [
            "",
            "## 三、Catalog 覆盖统计",
            f"- MCP target: {catalog_target}（{_safe(transport_summaries['mcp']['total'])}）",
            f"- Daemon target: {catalog_target}（{_safe(transport_summaries['daemon']['total'])}）",
            f"- MCP 本地有效: pass={transport_summaries['mcp']['pass']} scope_skip={transport_summaries['mcp']['scope_skip']} (effective={max(0, transport_summaries['mcp']['total'] - transport_summaries['mcp']['scope_skip'])})",
            f"- Daemon 本地有效: pass={transport_summaries['daemon']['pass']} scope_skip={transport_summaries['daemon']['scope_skip']} (effective={max(0, transport_summaries['daemon']['total'] - transport_summaries['daemon']['scope_skip'])})",
            "- rating:",
        ]
    )
    mcp_status = _overall_status(transport_summaries['mcp']['blocker'], transport_summaries['mcp']['issue'])
    daemon_status = _overall_status(transport_summaries['daemon']['blocker'], transport_summaries['daemon']['issue'])
    lines.extend(
        [
            f"  - MCP: {mcp_status}",
            f"  - Daemon: {daemon_status}",
        ]
    )

    lines.append("")
    lines.append("#### 合约明细")
    _append_stats(lines, "MCP", transport_summaries['mcp'], mcp_count)
    _append_stats(lines, "Daemon", transport_summaries['daemon'], daemon_count)

    lines.extend(
        [
            "",
            "## 四、Blocker / Issue 列表及复现命令",
            "### Blockers",
        ]
    )
    _append_blocklist(lines, all_blockers, "阻塞项")

    lines.extend(["### Issues", "- 主要按 issue_type 分组："])
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in all_issues:
        issue_type = str(item.get("issue_type") or "unknown")
        by_type[issue_type].append(item)
    if not by_type:
        lines.append("- (none)")
    else:
        for issue_type, grouped in sorted(by_type.items(), key=lambda i: i[0]):
            lines.append(f"#### {issue_type}")
            _append_blocklist(lines, grouped, "")

    lines.extend(
        [
            "### scope_skip 明细",
            f"- mcp: {len(mcp_scope_skip)}",
            f"- daemon: {len(daemon_scope_skip)}",
            "",
        ]
    )
    for item in (mcp_scope_skip + daemon_scope_skip):
        tool = item.get("tool")
        repro = item.get("repro_command") or ""
        lines.append(f"- {tool}: {item.get('matrix')} {item.get('transport')} | {repro}")

    lines.extend(
        [
            "",
            "## 五、清理结论",
            f"- {cleanup_status}",
        ]
    )
    if cleanup_reasons:
        lines.append("- 原因:")
        for reason in cleanup_reasons:
            lines.append(f"  - {reason}")

    write_text(out_path, "\n".join(lines) + "\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate rdx.bat and tool contract smoke outputs")
    parser.add_argument("--command-json", required=True)
    parser.add_argument("--tool-json", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--detailed-out", default="")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = _tools_root()

    command_path = resolve_repo_path(root, args.command_json)
    tool_path = resolve_repo_path(root, args.tool_json)

    command_payload = load_json(command_path.resolve())
    tool_payload = load_json(tool_path.resolve())

    if not command_payload:
        print(f"[aggregate] missing command payload: {command_path}")
        return 2
    if not tool_payload:
        print(f"[aggregate] missing tool payload: {tool_path}")
        return 2

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = root / out_path

    _write_report(out_path, command_payload, tool_payload)

    detailed_arg = str(args.detailed_out or "").strip()
    if detailed_arg:
        detailed_out = Path(detailed_arg)
        if not detailed_out.is_absolute():
            detailed_out = out_path.parent / detailed_out
    else:
        detailed_out = out_path.parent / "rdx_smoke_detailed_report.md"

    _write_report(detailed_out, command_payload, tool_payload)

    print(f"[aggregate] wrote: {out_path}")
    print(f"[aggregate] wrote detailed: {detailed_out}")

    command_summary = command_payload.get("summary", {}) if isinstance(command_payload.get("summary"), dict) else {}
    if int(command_summary.get("blocker", 0)) > 0:
        return 1

    for transport in ("mcp", "daemon"):
        transport_payload = tool_payload.get("transports", {}).get(transport, {})
        if not isinstance(transport_payload, dict):
            return 1
        summary = transport_payload.get("summary", {})
        if not isinstance(summary, dict):
            return 1
        if int(summary.get("blocker", 0)) > 0:
            return 1
        if str(transport_payload.get("fatal_error") or "").strip():
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

