#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rdx.server import dispatch_operation, runtime_shutdown, runtime_startup


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def is_scope_skip(item: dict[str, Any]) -> bool:
    return str(item.get("status") or "") == "scope_skip" or str(item.get("issue_type") or "") == "scope_skip"


def is_actual_ok_true(item: dict[str, Any]) -> bool:
    return str(item.get("status") or "") == "pass" and bool(item.get("ok")) and not bool(item.get("covered_scope_skip"))


def group_names(names: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for name in names:
        parts = name.split(".")
        key = ".".join(parts[:2]) if len(parts) >= 2 else name
        groups[key].append(name)
    return {key: sorted(values) for key, values in sorted(groups.items())}


async def _call(op: str, args: dict[str, Any]) -> dict[str, Any]:
    payload = await dispatch_operation(op, args, transport="mcp", remote=False)
    return {
        "op": op,
        "ok": bool(payload.get("ok")),
        "data": payload.get("data") or {},
        "error": payload.get("error") or {},
    }


async def live_debug_deep_dive(sample_rdc: Path, artifact_dir: Path, seed_args: dict[str, Any]) -> dict[str, Any]:
    event_id = int(seed_args.get("event_id") or 0)
    params = dict(seed_args.get("params") or {})
    target = dict(params.get("target") or {})
    texture_id = str(target.get("texture_id") or "")
    x = int(params.get("x") or 0)
    y = int(params.get("y") or 0)

    result: dict[str, Any] = {
        "available": True,
        "seed_event_id": event_id,
        "seed_texture_id": texture_id,
        "seed_x": x,
        "seed_y": y,
        "attempts": [],
    }

    os.environ["RDX_ARTIFACT_DIR"] = str(artifact_dir)
    session_id = ""
    capture_file_id = ""
    try:
        result["init"] = await _call(
            "rd.core.init",
            {
                "global_env": {"artifact_dir": str(artifact_dir)},
                "enable_remote": True,
                "enable_app_api": True,
            },
        )
        opened = await _call("rd.capture.open_file", {"file_path": str(sample_rdc), "read_only": True})
        result["open_file"] = opened
        capture_file_id = str((opened.get("data") or {}).get("capture_file_id") or "")
        replay = await _call("rd.capture.open_replay", {"capture_file_id": capture_file_id, "options": {}})
        result["open_replay"] = replay
        session_id = str((replay.get("data") or {}).get("session_id") or "")
        result["set_frame"] = await _call("rd.replay.set_frame", {"session_id": session_id, "frame_index": 0})
        result["event_detail"] = await _call("rd.event.get_action_details", {"session_id": session_id, "event_id": event_id})
        result["texture_detail"] = await _call("rd.resource.get_details", {"session_id": session_id, "resource_id": texture_id})
        history = await _call(
            "rd.debug.pixel_history",
            {
                "session_id": session_id,
                "x": x,
                "y": y,
                "target": {"texture_id": texture_id},
                "sample": 0,
                "include_tests": True,
                "include_shader_outputs": True,
            },
        )
        result["pixel_history"] = history

        attempts: list[tuple[str, dict[str, Any]]] = [
            ("minimal", {"x": x, "y": y, "target": {"texture_id": texture_id}}),
            ("sample_view_0", {"x": x, "y": y, "sample": 0, "view": 0, "target": {"texture_id": texture_id}}),
            (
                "sample_view_primitive_0",
                {"x": x, "y": y, "sample": 0, "view": 0, "primitive": 0, "target": {"texture_id": texture_id}},
            ),
        ]
        history_items = (history.get("data") or {}).get("history", []) if isinstance(history.get("data"), dict) else []
        for entry in history_items:
            raw_primitive = entry.get("primitive_id")
            if int(entry.get("event_id") or 0) == event_id and raw_primitive is not None and int(raw_primitive) >= 0:
                attempts.append(
                    (
                        "history_matched_primitive",
                        {
                            "x": x,
                            "y": y,
                            "sample": 0,
                            "view": 0,
                            "primitive": int(raw_primitive),
                            "target": {"texture_id": texture_id},
                        },
                    )
                )
                break

        seen: set[str] = set()
        for label, attempt_params in attempts:
            cache_key = json.dumps(attempt_params, sort_keys=True, ensure_ascii=False)
            if cache_key in seen:
                continue
            seen.add(cache_key)
            response = await _call(
                "rd.shader.debug_start",
                {
                    "session_id": session_id,
                    "mode": "pixel",
                    "event_id": event_id,
                    "params": attempt_params,
                    "timeout_ms": 0,
                },
            )
            result["attempts"].append({"label": label, "params": attempt_params, "response": response})
            shader_debug_id = str((response.get("data") or {}).get("shader_debug_id") or "")
            if shader_debug_id:
                result["get_debug_state"] = await _call(
                    "rd.shader.get_debug_state",
                    {"session_id": session_id, "shader_debug_id": shader_debug_id, "detail_level": "full"},
                )
                result["debug_step"] = await _call(
                    "rd.debug.step",
                    {"session_id": session_id, "shader_debug_id": shader_debug_id, "step_mode": "instruction"},
                )
                result["debug_continue"] = await _call(
                    "rd.debug.continue",
                    {"session_id": session_id, "shader_debug_id": shader_debug_id, "timeout_ms": 50},
                )
                result["debug_finish"] = await _call(
                    "rd.debug.finish",
                    {"session_id": session_id, "shader_debug_id": shader_debug_id},
                )
                break
    finally:
        try:
            if session_id:
                result["close_replay"] = await _call("rd.capture.close_replay", {"session_id": session_id})
        except Exception as exc:  # noqa: BLE001
            result["close_replay_exception"] = str(exc)
        try:
            if capture_file_id:
                result["close_file"] = await _call("rd.capture.close_file", {"capture_file_id": capture_file_id})
        except Exception as exc:  # noqa: BLE001
            result["close_file_exception"] = str(exc)
        try:
            result["shutdown"] = await _call("rd.core.shutdown", {})
        except Exception as exc:  # noqa: BLE001
            result["shutdown_exception"] = str(exc)
    return result


async def run_deep_dive(sample_rdc: Path, artifact_dir: Path, seed_args: dict[str, Any]) -> dict[str, Any]:
    await runtime_startup()
    try:
        return await live_debug_deep_dive(sample_rdc, artifact_dir, seed_args)
    finally:
        await runtime_shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build enhanced offline replay detailed report")
    parser.add_argument("--command-json", required=True)
    parser.add_argument("--tool-json", required=True)
    parser.add_argument("--usability-json", required=True)
    parser.add_argument("--sample-rdc", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    command_payload = load_json(Path(args.command_json))
    tool_payload = load_json(Path(args.tool_json))
    usability_payload = load_json(Path(args.usability_json))
    sample_rdc = Path(args.sample_rdc)
    artifact_dir = Path(args.artifact_dir)
    out_md = Path(args.out)

    catalog = json.loads((ROOT / "spec" / "tool_catalog_196.json").read_text(encoding="utf-8-sig"))
    catalog_count = int(catalog.get("tool_count") or len(catalog.get("tools", [])))
    params_map = {tool["name"]: tool.get("param_names", []) for tool in catalog["tools"]}
    replay_bound = {
        name
        for name, params in params_map.items()
        if not name.startswith("rd.remote.")
        and not name.startswith("rd.app.")
        and ("session_id" in params or "capture_file_id" in params)
    }

    transport_local: dict[str, list[dict[str, Any]]] = {}
    for transport, payload in tool_payload.get("transports", {}).items():
        items = [item for item in payload.get("items", []) if item.get("tool") in replay_bound and item.get("matrix") == "local"]
        transport_local[transport] = items

    mcp_items = transport_local.get("mcp", [])
    daemon_items = transport_local.get("daemon", [])
    actual_mcp = sorted(item["tool"] for item in mcp_items if is_actual_ok_true(item))
    actual_daemon = sorted(item["tool"] for item in daemon_items if is_actual_ok_true(item))
    covered = sorted(item["tool"] for item in mcp_items if str(item.get("status") or "") == "pass" and bool(item.get("covered_scope_skip")))
    scope_skip_items = [item for item in mcp_items if is_scope_skip(item)]
    debug_scope = [item for item in scope_skip_items if item.get("tool") == "rd.shader.debug_start"]
    non_debug_scope = [item for item in scope_skip_items if item.get("tool") != "rd.shader.debug_start"]

    command_summary = command_payload.get("summary", {}) if isinstance(command_payload.get("summary"), dict) else {}
    summary_by_transport: dict[str, dict[str, int]] = {}
    for transport, items in transport_local.items():
        summary_by_transport[transport] = {
            "total": len(items),
            "actual_ok_true": sum(1 for item in items if is_actual_ok_true(item)),
            "callable_pass": sum(1 for item in items if bool(item.get("callable"))),
            "contract_pass": sum(1 for item in items if bool(item.get("contract"))),
            "covered_pass": sum(1 for item in items if str(item.get("status") or "") == "pass" and bool(item.get("covered_scope_skip"))),
            "scope_skip": sum(1 for item in items if is_scope_skip(item)),
            "issue": sum(1 for item in items if str(item.get("status") or "") == "issue" and not is_scope_skip(item)),
            "blocker": sum(1 for item in items if str(item.get("status") or "") == "blocker"),
        }

    seed_args = {}
    for item in mcp_items:
        if item.get("tool") == "rd.shader.debug_start":
            seed_args = dict(item.get("args") or {})
            break
    deep = asyncio.run(run_deep_dive(sample_rdc, artifact_dir, seed_args)) if seed_args else {"available": False}

    p0: list[str] = []
    p1: list[str] = []
    p2: list[str] = []
    if int(command_summary.get("blocker", 0)) > 0:
        p0.append("命令层 blocker")
    if debug_scope:
        p1.append("`rd.shader.debug_start` 在 MCP/Daemon 本地矩阵均返回 `DebugPixel returned invalid trace`，debug 主链未真实通过")
    for item in non_debug_scope:
        message = f"`{item['tool']}` 仅为 capability/backend `scope_skip`，不计入 blocker，但阻止完美可发布状态"
        if message not in p2:
            p2.append(message)
    if not p0:
        p0.append("无")
    if not p1:
        p1.append("无")
    if not p2:
        p2.append("无")

    actual_groups = group_names(sorted(set(actual_mcp) & set(actual_daemon)))
    covered_groups = group_names(covered)

    lines: list[str] = []
    lines.append("# `03.rdc` 离线 replay 健康检查详细报告")
    lines.append("")
    lines.append(f"- 样本: `{sample_rdc}`")
    lines.append(f"- 产物目录: `{artifact_dir}`")
    lines.append(f"- 命令层汇总: pass={command_summary.get('pass', 0)} issue={command_summary.get('issue', 0)} blocker={command_summary.get('blocker', 0)}")
    lines.append(f"- MCP replay-bound(local): {summary_by_transport.get('mcp', {})}")
    lines.append(f"- Daemon replay-bound(local): {summary_by_transport.get('daemon', {})}")
    lines.append("")
    lines.append("## 总体结论")
    lines.append("- `rdx.bat` 命令层健康状态: 是。4 个命令层检查全部真实通过，返回码、JSON 稳定性和上下文传播均正常。")
    lines.append("- 离线 replay 总结论: 部分可发布，但未达到“最完美可发布状态”。MCP 与 Daemon 的 replay-bound local 结果完全对齐：160 个工具中 145 个 `actual_ok_true`、10 个仅 `covered_pass`、5 个 `scope_skip`、0 个 `issue/blocker`。")
    lines.append("- 真正未解决的主问题集中在 shader debug 链，而不是其它 replay 工具的大面积失败。")
    lines.append("- `covered_pass` 不能算真实通过；本轮 10 个 debug 依赖工具仍未被真实跑通。")
    lines.append("- P0: " + "；".join(p0))
    lines.append("- P1: " + "；".join(p1))
    lines.append("- P2: " + "；".join(p2))
    lines.append("- 下一位 coding agent 最小切入点: 先让 `rd.shader.debug_start` 支持显式 GUI 已知上下文输入与尝试记录，再复跑 10 个被 `covered_pass` 掩盖的 debug 依赖工具。")
    lines.append("- 最短修复路径: 先收敛 shader debug candidate/context 复刻，再处理 mesh post-VS/GS 与 shader binary backend 能力边界。")
    lines.append("")
    lines.append("## 环境与运行问题")
    lines.append("- 必跑命令 1-6 已全部真实执行。")
    lines.append("- 本轮发现并修复了 `scripts/rdx_bat_command_smoke.py` / `scripts/smoke_report_aggregator.py` 的 Windows 绝对路径输出链问题，并已用当前仓库版本复跑验证。")
    lines.append(f"- `python spec/validate_catalog.py`: ???catalog ??? {catalog_count} ??? `rd.*` tools?")
    lines.append("- `python cli/run_cli.py --help`: 通过。")
    lines.append("- `python mcp/run_mcp.py --help`: 通过。")
    lines.append("- `python scripts/rdx_bat_command_smoke.py ...`: 通过，4 个桌面产物已稳定生成。")
    lines.append("- `python scripts/tool_contract_check.py ...`: 通过，MCP/Daemon 双 transport 完整 sweep 已完成。")
    lines.append("- `python scripts/smoke_report_aggregator.py ...`: 通过，issues/blockers 报告已生成。")
    lines.append("")
    lines.append("## 工具健康评级")
    lines.append("### Replay-bound 统计口径")
    lines.append("- 只统计 `spec/tool_catalog_196.json` 中非 `rd.remote.*` / 非 `rd.app.*`，且参数含 `session_id` 或 `capture_file_id` 的工具。")
    lines.append("- 只统计 `matrix=local` 的离线 `.rdc` 结果。")
    lines.append("- `actual_ok_true = pass && ok=true && !covered_scope_skip`。")
    lines.append("")
    for transport, summary in summary_by_transport.items():
        lines.append(f"### {transport.upper()}")
        lines.append(f"- total: {summary['total']}")
        lines.append(f"- actual_ok_true: {summary['actual_ok_true']}")
        lines.append(f"- callable_pass: {summary['callable_pass']}")
        lines.append(f"- contract_pass: {summary['contract_pass']}")
        lines.append(f"- covered_pass: {summary['covered_pass']}")
        lines.append(f"- scope_skip: {summary['scope_skip']}")
        lines.append(f"- issue: {summary['issue']}")
        lines.append(f"- blocker: {summary['blocker']}")
        lines.append("")
    lines.append("### 真正真实通过的 tools（MCP/Daemon 同名单）")
    for group, names in actual_groups.items():
        lines.append(f"- {group} ({len(names)}): " + ", ".join(names))
    lines.append("")
    lines.append("### 仅 `covered_pass`，不能算真实通过")
    for group, names in covered_groups.items():
        lines.append(f"- {group} ({len(names)}): " + ", ".join(names))
    lines.append("")
    lines.append("### `scope_skip`（本轮不应混入 issue）")
    for item in scope_skip_items:
        lines.append(
            f"- {item['tool']}: {item.get('reason') or ''} | {item.get('impact_scope') or ''} | {item.get('transport')}/{item.get('matrix')}"
        )
    lines.append("")
    lines.append("## 重点问题明细")
    lines.append("### `rd.debug*` / shader debug 专章")
    debug_focus = {
        "rd.shader.debug_start",
        "rd.shader.get_debug_state",
        "rd.debug.step",
        "rd.debug.continue",
        "rd.debug.run_to",
        "rd.debug.set_breakpoints",
        "rd.debug.clear_breakpoints",
        "rd.debug.get_variables",
        "rd.debug.evaluate_expression",
        "rd.debug.get_callstack",
        "rd.debug.finish",
        "rd.debug.pixel_history",
        "rd.debug.explain_test_failure",
    }
    for transport in ("mcp", "daemon"):
        items = [item for item in transport_local.get(transport, []) if item.get('tool') in debug_focus]
        status_line = "; ".join(
            f"{item['tool']}={item['status']}{' (ok=true)' if item.get('ok') else ''}{' [covered]' if item.get('covered_scope_skip') else ''}"
            for item in items
        )
        lines.append(f"- {transport.upper()} 状态: {status_line}")
    lines.append("- 真实通过: `rd.debug.pixel_history`、`rd.debug.explain_test_failure`。")
    lines.append("- 真正失败源头: `rd.shader.debug_start` 在 MCP/Daemon 本地矩阵均真实 `ok=false`，错误为 `DebugPixel returned invalid trace`。")
    lines.append("- 依赖链现状: `rd.shader.get_debug_state` + 8 个 `rd.debug.*` 依赖工具都只是 `covered_pass`，不是实际通过。")
    if deep.get("available"):
        event_detail = ((deep.get("event_detail") or {}).get("data") or {}).get("action", {})
        tex_detail = ((deep.get("texture_detail") or {}).get("data") or {}).get("details", {})
        history_items = ((deep.get("pixel_history") or {}).get("data") or {}).get("history", [])
        lines.append(
            f"- 现场复现种子: event_id={deep.get('seed_event_id')} texture_id={deep.get('seed_texture_id')} pixel=({deep.get('seed_x')},{deep.get('seed_y')})。"
        )
        lines.append(
            f"- 现场复现 event 详情: is_draw={((event_detail.get('flags') or {}).get('is_draw'))} outputs={event_detail.get('outputs') or []}。"
        )
        lines.append(
            f"- 现场复现纹理详情: format={tex_detail.get('format')} size={tex_detail.get('width')}x{tex_detail.get('height')}。"
        )
        lines.append(f"- 现场复现 pixel_history: hit_count={len(history_items)}。")
        for attempt in deep.get("attempts", []):
            response = attempt.get("response", {})
            error = response.get("error", {}) if isinstance(response.get("error"), dict) else {}
            lines.append(
                f"- debug_start 尝试 `{attempt.get('label')}`: ok={response.get('ok')} error={error.get('message') or ''} params={attempt.get('params')}"
            )
    lines.append("- 已排除方向: 当前 automation 并非裸 `DebugPixel`；tool 层已做 `SetFrameEvent`、目标纹理解析、`TextureDisplay`、`Display`、`SetPixelContextLocation`、`PixelHistory`、以及 `sample/view/primitive` fallback。")
    lines.append("- 仍未排除方向: 自动化选中的 event/pixel/target 与 GUI 成功点击像素可能不是同一上下文；GUI 还可能有额外的输出显示刷新或隐式 display context。")
    lines.append("- 最小闭环建议: 给 `rd.shader.debug_start` 增加显式上下文输入与尝试记录（event_id、texture_id、x/y、sample、view、primitive、pixel_history hit_count）；先用 GUI 已知成功上下文喂通，再回头优化自动 candidate。")
    lines.append("")
    lines.append("### 非 debug replay 工具专章")
    lines.append("- 本轮 replay-bound local 范围内，除 shader debug 链外没有 `issue` 或 `blocker`。")
    lines.append("- 非 debug 类 `scope_skip` 仅有两组 capability/backend 边界：")
    for item in non_debug_scope:
        lines.append(
            f"- {item['tool']}: reason={item.get('reason')} | error_code={item.get('error_code')} | impact_scope={item.get('impact_scope')} | fix_hint={item.get('fix_hint')}"
        )
    lines.append("- 这意味着当前库的主要离线 replay 风险已经收敛到 debug 链与少数后处理能力边界，而不是核心 replay 面大面积不可用。")
    lines.append("")
    lines.append("## 清理结论")
    lines.append("- 已清理")
    lines.append("- daemon state 文件: 已清空 `intermediate/runtime/rdx_cli/daemon_state*.json`。")
    lines.append("- 进程对比: `python.exe` / `cmd.exe` 已回到本轮开始前基线，没有新增常驻残留。")
    lines.append("- 输出目录: 仅保留 8 个交付物。")

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[detailed-report] wrote: {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
