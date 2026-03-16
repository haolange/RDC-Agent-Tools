from __future__ import annotations

import argparse
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree


_TOOL_NAME_RE = re.compile(r"^rd\.[a-z]+\.[a-z0-9_]+$")
_HEADING_RE = re.compile(r"^3\.\d+")
_PARAM_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_SECTION_HEADING_RE = re.compile(r"^[\u4e00-\u9fff]+[\u3001\uff0c]")
_WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
_CORE_GROUP = "3.1\uff0c\u6838\u5fc3\u4e0e\u73af\u5883\u7ba1\u7406 (Core & Environment)"
_CONTEXT_GROUP = "3.17\uff0c\u4e0a\u4e0b\u6587\u5feb\u7167\u5de5\u5177 (Context Snapshot Tools)"
_VFS_GROUP = "3.18\uff0cVFS \u5bfc\u822a\u5de5\u5177 (VFS Navigation Tools)"
_OVERLAY_PATH = Path(__file__).resolve().with_name("tool_catalog_overlay.json")
_MANUAL_TOOLS = [
    {
        "name": "rd.session.get_context",
        "group": _CONTEXT_GROUP,
        "description": "\u8bfb\u53d6\u5f53\u524d context \u5feb\u7167\u4e0e\u6301\u4e45\u5316\u72b6\u6001\u7d22\u5f15\uff0c\u8fd4\u56de runtime\u3001remote\u3001focus\u3001session \u8868\u3001\u6062\u590d\u4fe1\u606f\u3001\u6700\u8fd1\u64cd\u4f5c\u4e0e\u9650\u5236\u914d\u7f6e\u3002",
        "parameter_raw": "",
        "returns_raw": "ok (bool)<br>data (dict): {context_id, runtime, remote, focus, last_artifacts, current_session_id, sessions, recovery, limits, active_operation, recent_operations, updated_at_ms}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)<br>projections (dict, \u53ef\u9009)",
    },
    {
        "name": "rd.session.update_context",
        "group": _CONTEXT_GROUP,
        "description": "\u66f4\u65b0\u5f53\u524d context \u7684 user-owned \u5b57\u6bb5\uff0c\u4f8b\u5982 focus_pixel\u3001focus_resource_id\u3001focus_shader_id \u4e0e notes\u3002",
        "parameter_raw": "key (str)<br>value (json, \u53ef\u9009): \u4f20 null \u8868\u793a\u6e05\u9664\u5bf9\u5e94 user-owned \u5b57\u6bb5",
        "returns_raw": "ok (bool)<br>data (dict): {context_id, runtime, remote, focus, notes, last_artifacts, updated_at_ms}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)<br>projections (dict, \u53ef\u9009)",
    },
    {
        "name": "rd.session.list_sessions",
        "group": _CONTEXT_GROUP,
        "description": "\u5217\u51fa\u5f53\u524d context \u4e0b\u7684\u6301\u4e45\u5316 session \u8868\u3001\u5f53\u524d\u9009\u4e2d session \u4e0e\u6062\u590d\u6458\u8981\u3002",
        "parameter_raw": "",
        "returns_raw": "ok (bool)<br>data (dict): {context_id, current_session_id, sessions, recovery, limits}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)<br>projections (dict, \u53ef\u9009)",
    },
    {
        "name": "rd.session.select_session",
        "group": _CONTEXT_GROUP,
        "description": "\u5207\u6362\u5f53\u524d context \u7684 current session \u6307\u9488\uff0c\u800c\u4e0d\u9500\u6bc1\u5176\u4ed6\u5df2\u6301\u6709\u7684 session\u3002",
        "parameter_raw": "session_id (str)",
        "returns_raw": "ok (bool)<br>data (dict): {context_id, runtime, remote, focus, current_session_id, sessions, recovery, limits, recent_operations, updated_at_ms}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)<br>projections (dict, \u53ef\u9009)",
        "prerequisites": [],
    },
    {
        "name": "rd.session.resume",
        "group": _CONTEXT_GROUP,
        "description": "\u57fa\u4e8e\u6301\u4e45\u5316\u72b6\u6001\u5c1d\u8bd5\u6062\u590d\u5f53\u524d context \u7684\u672c\u5730 `.rdc` session\uff1b\u53ef\u9009\u4ec5\u6821\u9a8c\u6307\u5b9a session \u7684\u6062\u590d\u7ed3\u679c\u3002",
        "parameter_raw": "session_id (str, \u53ef\u9009): \u82e5\u7ed9\u5b9a\u5219\u5728\u6062\u590d\u5b8c\u6210\u540e\u6821\u9a8c\u8be5 session \u662f\u5426\u5df2\u6062\u590d\u4e3a live",
        "returns_raw": "ok (bool)<br>data (dict): {context_id, runtime, remote, focus, current_session_id, sessions, recovery, limits, recent_operations, updated_at_ms}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)<br>projections (dict, \u53ef\u9009)",
        "prerequisites": [],
    },
    {
        "name": "rd.core.get_operation_history",
        "group": _CORE_GROUP,
        "description": "\u8bfb\u53d6\u5f53\u524d context \u6700\u8fd1\u7684 trace-linked \u64cd\u4f5c\u5386\u53f2\uff0c\u53ef\u6309\u65f6\u95f4\u3001\u72b6\u6001\u4e0e\u64cd\u4f5c\u540d\u8fc7\u6ee4\u3002",
        "parameter_raw": "since_ms (int, \u53ef\u9009): \u4ec5\u8fd4\u56de\u8be5\u65f6\u95f4\u6233\u4e4b\u540e\u66f4\u65b0\u8fc7\u7684\u64cd\u4f5c<br>operation (str, \u53ef\u9009): \u6309\u64cd\u4f5c\u540d\u5b50\u4e32\u8fc7\u6ee4<br>status (str, \u53ef\u9009): \u6309 running/completed/failed \u8fc7\u6ee4<br>max_items (int, \u53ef\u9009, \u9ed8\u8ba4 32)",
        "returns_raw": "ok (bool)<br>data (dict): {context_id, operations}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)<br>projections (dict, \u53ef\u9009)",
    },
    {
        "name": "rd.core.get_runtime_metrics",
        "group": _CORE_GROUP,
        "description": "\u8bfb\u53d6\u5f53\u524d context \u7684\u81ea\u76d1\u63a7\u6307\u6807\u3001\u9650\u5236\u914d\u7f6e\u3001\u6062\u590d\u6458\u8981\u4e0e\u6700\u8fd1\u64cd\u4f5c\u7edf\u8ba1\u3002",
        "parameter_raw": "",
        "returns_raw": "ok (bool)<br>data (dict): {context_id, limits, metrics, recovery, recent_operations}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)<br>projections (dict, \u53ef\u9009)",
    },
    {
        "name": "rd.core.list_tools",
        "group": _CORE_GROUP,
        "description": "\u6309 namespace\u3001group\u3001capability\u3001role\u3001intent \u7b49\u7ed3\u6784\u5316\u6761\u4ef6\u5217\u51fa\u53ef\u7528 tool\uff0c\u907f\u514d\u4e00\u6b21\u6027\u6ce8\u5165\u5168\u90e8\u63cf\u8ff0\u3002",
        "parameter_raw": "namespace (str, \u53ef\u9009)<br>group (str, \u53ef\u9009)<br>capability (str, \u53ef\u9009)<br>role (str, \u53ef\u9009): canonical|macro|navigation<br>intent (str, \u53ef\u9009)<br>mutates_state (bool, \u53ef\u9009)<br>detail_level (str, \u53ef\u9009, \u9ed8\u8ba4 'summary'): summary|full",
        "returns_raw": "ok (bool)<br>data (dict): {tool_count, tools}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)<br>projections (dict, \u53ef\u9009)",
    },
    {
        "name": "rd.core.search_tools",
        "group": _CORE_GROUP,
        "description": "\u6309\u540d\u79f0\u3001\u63cf\u8ff0\u3001group\u3001capability \u4e0e intent \u7684\u7ed3\u6784\u5316\u5b50\u4e32\u641c\u7d22 tool\uff0c\u8fd4\u56de\u9002\u914d Agent \u7684\u8f7b\u91cf\u89c6\u56fe\u3002",
        "parameter_raw": "query (str, \u53ef\u9009)<br>namespace (str, \u53ef\u9009)<br>capability (str, \u53ef\u9009)<br>role (str, \u53ef\u9009): canonical|macro|navigation<br>intent (str, \u53ef\u9009)<br>detail_level (str, \u53ef\u9009, \u9ed8\u8ba4 'summary'): summary|full",
        "returns_raw": "ok (bool)<br>data (dict): {tool_count, tools}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)<br>projections (dict, \u53ef\u9009)",
    },
    {
        "name": "rd.core.get_tool_graph",
        "group": _CORE_GROUP,
        "description": "\u8fd4\u56de tool \u4e4b\u95f4\u7684 prerequisite \u4e0e macro-to-canonical \u4f9d\u8d56\u56fe\uff0c\u5e2e\u52a9 Agent \u63a8\u65ad\u8c03\u7528\u94fe\u4e0e\u5c55\u5f00\u8def\u5f84\u3002",
        "parameter_raw": "query (str, \u53ef\u9009)<br>namespace (str, \u53ef\u9009)<br>capability (str, \u53ef\u9009)<br>role (str, \u53ef\u9009): canonical|macro|navigation<br>intent (str, \u53ef\u9009)",
        "returns_raw": "ok (bool)<br>data (dict): {tools, edges}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)<br>projections (dict, \u53ef\u9009)",
    },
    {
        "name": "rd.vfs.ls",
        "group": _VFS_GROUP,
        "description": "\u4ee5 JSON-first \u65b9\u5f0f\u5217\u51fa read-only VFS \u8282\u70b9\uff0c\u7528\u4e8e\u63a2\u7d22 draws/passes/resources/context/artifacts \u7b49\u8def\u5f84\u7a7a\u95f4\u3002",
        "parameter_raw": "path (str, \u53ef\u9009, \u9ed8\u8ba4 '/'): VFS \u8def\u5f84<br>session_id (str, \u53ef\u9009): \u5f53 path \u6307\u5411 replay \u76f8\u5173\u57df\u65f6\u7528\u4e8e\u89e3\u6790\u5f53\u524d session<br>projection (dict, \u53ef\u9009): \u5f53 kind='tabular' \u65f6\u8fd4\u56de entries \u7684\u7edf\u4e00\u8868\u683c\u6458\u8981",
        "returns_raw": "ok (bool)<br>data (dict): {path, node, entries}<br>projections.tabular (dict, \u53ef\u9009): {format_version, columns, rows, row_count, tsv_text?}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)",
        "supports_projection": {"tabular": True},
        "prerequisites": [],
    },
    {
        "name": "rd.vfs.cat",
        "group": _VFS_GROUP,
        "description": "\u8bfb\u53d6 read-only VFS \u8282\u70b9\u7684 JSON \u8868\u793a\uff0c\u4e0d\u65b0\u589e\u7b2c\u4e8c\u5957\u5e73\u884c\u771f\u76f8\uff0c\u800c\u662f\u5bf9\u5e95\u5c42 rd.* \u80fd\u529b\u7684\u5bfc\u822a\u5c01\u88c5\u3002",
        "parameter_raw": "path (str): VFS \u8def\u5f84<br>session_id (str, \u53ef\u9009): \u5f53 path \u6307\u5411 replay \u76f8\u5173\u57df\u65f6\u7528\u4e8e\u89e3\u6790\u5f53\u524d session",
        "returns_raw": "ok (bool)<br>data (dict): {path, node}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)",
        "prerequisites": [],
    },
    {
        "name": "rd.vfs.tree",
        "group": _VFS_GROUP,
        "description": "\u6309\u7167 VFS \u8def\u5f84\u8fd4\u56de\u6811\u5f62 read-only \u89c6\u56fe\uff0c\u9ed8\u8ba4\u4ee5\u7ed3\u6784\u5316 JSON \u8868\u793a\u8282\u70b9\u4e0e children\u3002",
        "parameter_raw": "path (str, \u53ef\u9009, \u9ed8\u8ba4 '/'): VFS \u8d77\u70b9\u8def\u5f84<br>depth (int, \u53ef\u9009, \u9ed8\u8ba4 2): \u9012\u5f52\u6df1\u5ea6<br>session_id (str, \u53ef\u9009): \u5f53 path \u6307\u5411 replay \u76f8\u5173\u57df\u65f6\u7528\u4e8e\u89e3\u6790\u5f53\u524d session",
        "returns_raw": "ok (bool)<br>data (dict): {path, tree}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)",
        "prerequisites": [],
    },
    {
        "name": "rd.vfs.resolve",
        "group": _VFS_GROUP,
        "description": "\u89e3\u6790 VFS \u8def\u5f84\u5230\u5bf9\u5e94\u8282\u70b9\u5143\u6570\u636e\uff0c\u7528\u4e8e\u5224\u65ad path \u662f\u5426\u5b58\u5728\u3001\u662f\u5426\u9700\u8981 session \u4ee5\u53ca\u53ef\u4f7f\u7528\u54ea\u7c7b\u89c6\u56fe\u3002",
        "parameter_raw": "path (str): VFS \u8def\u5f84<br>session_id (str, \u53ef\u9009): \u5f53 path \u6307\u5411 replay \u76f8\u5173\u57df\u65f6\u7528\u4e8e\u89e3\u6790\u5f53\u524d session",
        "returns_raw": "ok (bool)<br>data (dict): {path, node}<br>artifacts (list)<br>error (dict|null)<br>meta (dict)",
        "prerequisites": [],
    },
]
_STATE_PREREQUISITES = {
    "capture_file_id": {
        "requires": "capture_file_id",
        "via_tools": ["rd.capture.open_file"],
        "reason": "This tool requires an opened capture handle before it can act on capture-backed state.",
    },
    "session_id": {
        "requires": "session_id",
        "via_tools": ["rd.capture.open_file", "rd.capture.open_replay"],
        "reason": "This tool operates on a live replay session.",
    },
    "remote_id": {
        "requires": "remote_id",
        "via_tools": ["rd.remote.connect"],
        "reason": "This tool targets a live remote endpoint handle.",
    },
}


def _extract_param_names(param_line: str) -> List[str]:
    names: List[str] = []
    for piece in param_line.replace("<2>", "<br>").split("<br>"):
        piece = piece.strip()
        if not piece:
            continue
        match = _PARAM_NAME_RE.match(piece)
        if match:
            name = match.group(1)
            if name not in names:
                names.append(name)
    return names


def _normalize_returns_raw(returns_raw: str) -> str:
    text = str(returns_raw or "")
    if "data (dict)" in text and "error (dict" in text and "meta (dict)" in text:
        return text
    fields: List[str] = []
    for piece in text.replace("<2>", "<br>").split("<br>"):
        piece = piece.strip()
        if not piece:
            continue
        match = _PARAM_NAME_RE.match(piece)
        if not match:
            continue
        name = match.group(1)
        if name in {"success", "ok", "error_message"}:
            continue
        if name not in fields:
            fields.append(name)
    if fields:
        data_desc = "{%s}" % ", ".join(fields)
    else:
        data_desc = "tool-specific payload"
    return (
        "ok (bool)<br>"
        f"data (dict): {data_desc}<br>"
        "artifacts (list)<br>"
        "error (dict|null)<br>"
        "meta (dict)<br>"
        "projections (dict, 可选)"
    )


def _text_from_node(node: ElementTree.Element) -> str:
    texts = [t.text or "" for t in node.findall(".//w:t", _WORD_NS)]
    return "".join(texts).strip()


def _iter_docx_tool_rows(docx_path: Path) -> List[Dict[str, str]]:
    with zipfile.ZipFile(docx_path) as archive:
        xml = archive.read("word/document.xml")

    root = ElementTree.fromstring(xml)
    body = root.find("w:body", _WORD_NS)
    if body is None:
        return []

    current_group = ""
    rows: List[Dict[str, str]] = []

    for elem in body:
        if elem.tag == f"{{{_WORD_NS['w']}}}p":
            text = _text_from_node(elem)
            if _HEADING_RE.match(text):
                current_group = text
            continue

        if elem.tag != f"{{{_WORD_NS['w']}}}tbl":
            continue

        for row in elem.findall("w:tr", _WORD_NS):
            cells: List[str] = []
            for cell in row.findall("w:tc", _WORD_NS):
                cells.append(_text_from_node(cell))
            if len(cells) != 4:
                continue
            name = cells[0]
            if not _TOOL_NAME_RE.fullmatch(name):
                continue
            rows.append(
                {
                    "group": current_group,
                    "name": name,
                    "description": cells[1],
                    "parameter_raw": cells[2],
                    "returns_raw": cells[3],
                },
            )

    return rows


def _iter_text_tool_rows(text_path: Path) -> List[Dict[str, str]]:
    current_group = ""
    rows: List[Dict[str, str]] = []
    lines: List[str] = []
    for raw in text_path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\d+:\s*(.*)$", raw)
        if not match:
            continue
        line = match.group(1).strip()
        if line:
            lines.append(line)

    index = 0
    while index < len(lines):
        line = lines[index]
        if _HEADING_RE.match(line):
            current_group = line
            index += 1
            continue
        if _TOOL_NAME_RE.fullmatch(line):
            name = line
            if index + 1 >= len(lines):
                break
            description = lines[index + 1].strip()
            index += 2

            param_parts: List[str] = []
            while index < len(lines):
                if lines[index].startswith("success (bool)"):
                    break
                if _TOOL_NAME_RE.fullmatch(lines[index]) or _HEADING_RE.match(lines[index]) or _SECTION_HEADING_RE.match(lines[index]):
                    break
                param_parts.append(lines[index].strip())
                index += 1

            return_parts: List[str] = []
            while index < len(lines):
                if _TOOL_NAME_RE.fullmatch(lines[index]) or _HEADING_RE.match(lines[index]) or _SECTION_HEADING_RE.match(lines[index]):
                    break
                return_parts.append(lines[index].strip())
                index += 1

            rows.append(
                {
                    "group": current_group,
                    "name": name,
                    "description": description,
                    "parameter_raw": "<br>".join(part for part in param_parts if part),
                    "returns_raw": "<br>".join(part for part in return_parts if part),
                },
            )
            continue
        index += 1

    return rows


def _normalize_source_path(source_path: Path, repo_root: Path) -> str:
    try:
        return source_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return source_path.name


def _load_overlay() -> Dict[str, Any]:
    if not _OVERLAY_PATH.is_file():
        return {"tools": {}}
    payload = json.loads(_OVERLAY_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"tools": {}}
    tools = payload.get("tools")
    if not isinstance(tools, dict):
        payload["tools"] = {}
    return payload


def _infer_prerequisites(tool_name: str, param_names: List[str]) -> List[Dict[str, Any]]:
    prerequisites: List[Dict[str, Any]] = []
    for param_name in param_names:
        item = _STATE_PREREQUISITES.get(param_name)
        if item is None:
            continue
        prerequisites.append(dict(item))
    if tool_name.startswith("rd.remote.") and tool_name != "rd.core.init":
        prerequisites.append(
            {
                "requires": "capability.remote",
                "via_tools": ["rd.core.init"],
                "reason": "Remote tools require remote capability to be enabled for the current runtime.",
            }
        )
    if tool_name.startswith("rd.app."):
        prerequisites.append(
            {
                "requires": "capability.app_api",
                "via_tools": ["rd.core.init"],
                "reason": "In-process app tools require app API integration to be enabled.",
            }
        )
    unique: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in prerequisites:
        key = (str(item.get("requires") or ""), str(item.get("when") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _apply_overlay(tools: List[Dict[str, Any]], overlay: Dict[str, Any]) -> List[Dict[str, Any]]:
    tool_overrides = overlay.get("tools") if isinstance(overlay, dict) else {}
    if not isinstance(tool_overrides, dict):
        return tools
    updated: List[Dict[str, Any]] = []
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        override = tool_overrides.get(name)
        if isinstance(override, dict):
            merged = dict(tool)
            for key, value in override.items():
                merged[key] = value
            updated.append(merged)
        else:
            updated.append(tool)
    return updated


def build_catalog(source_path: Path, output_path: Path) -> Dict[str, Any]:
    repo_root = output_path.resolve().parents[1]
    if source_path.suffix.lower() == ".docx":
        rows = _iter_docx_tool_rows(source_path)
    else:
        rows = _iter_text_tool_rows(source_path)

    tools: List[Dict[str, Any]] = []
    for row in rows:
        parameter_raw = row["parameter_raw"]
        tool_payload = {
            "name": row["name"],
            "group": row["group"],
            "description": row["description"],
            "parameter_raw": parameter_raw,
            "returns_raw": _normalize_returns_raw(row["returns_raw"]),
            "param_names": _extract_param_names(parameter_raw),
            "prerequisites": list(row["prerequisites"]) if isinstance(row.get("prerequisites"), list) else _infer_prerequisites(row["name"], _extract_param_names(parameter_raw)),
        }
        for extra_key in ("supports_projection",):
            if extra_key in row:
                tool_payload[extra_key] = row[extra_key]
        tools.append(tool_payload)

    existing = {tool["name"] for tool in tools}
    for row in _MANUAL_TOOLS:
        if row["name"] in existing:
            continue
        parameter_raw = row["parameter_raw"]
        tool_payload = {
            "name": row["name"],
            "group": row["group"],
            "description": row["description"],
            "parameter_raw": parameter_raw,
            "returns_raw": _normalize_returns_raw(row["returns_raw"]),
            "param_names": _extract_param_names(parameter_raw),
            "prerequisites": list(row["prerequisites"]) if isinstance(row.get("prerequisites"), list) else _infer_prerequisites(row["name"], _extract_param_names(parameter_raw)),
        }
        for extra_key in ("supports_projection",):
            if extra_key in row:
                tool_payload[extra_key] = row[extra_key]
        tools.append(tool_payload)

    tools = _apply_overlay(tools, _load_overlay())

    payload: Dict[str, Any] = {
        "source_path": _normalize_source_path(source_path, repo_root),
        "tool_count": len(tools),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "groups": dict(Counter(tool["group"] for tool in tools)),
        "tools": tools,
    }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build tool catalog for rdx-tools")
    parser.add_argument("--source", default="spec/doc_extracted.txt", help="Source text/docx path. Defaults to the repo-local extracted text.")
    parser.add_argument("--out", default="spec/tool_catalog.json", help="Catalog output path. Defaults to spec/tool_catalog.json.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    source_arg = Path(args.source)
    out_arg = Path(args.out)
    source = (repo_root / source_arg).resolve() if not source_arg.is_absolute() else source_arg.resolve()
    output = (repo_root / out_arg).resolve() if not out_arg.is_absolute() else out_arg.resolve()
    if not source.is_file():
        print(f"[spec] Missing source file: {source}")
        return 1

    payload = build_catalog(source, output)
    count = int(payload["tool_count"])
    print(f"[spec] Built catalog: {count} tools -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
