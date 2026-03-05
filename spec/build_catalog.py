from __future__ import annotations

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
_WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _strip_prefix(raw: str) -> str:
    match = re.match(r"\d+:\s*\d+:\s*(.*)$", raw)
    if match:
        return match.group(1).strip()
    match = re.match(r"\d+:\s*(.*)$", raw)
    if match:
        return match.group(1).strip()
    return raw.strip()


def _extract_param_names(param_line: str) -> List[str]:
    names: List[str] = []
    for piece in param_line.replace("<2>", "<br>").split("<br>"):
        piece = piece.strip()
        if not piece:
            continue
        m = _PARAM_NAME_RE.match(piece)
        if m:
            name = m.group(1)
            if name not in names:
                names.append(name)
    return names


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


def build_catalog(
    docx_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    rows = _iter_docx_tool_rows(docx_path)

    tools: List[Dict[str, Any]] = []
    for row in rows:
        parameter_raw = row["parameter_raw"]
        param_names = _extract_param_names(parameter_raw)

        tools.append(
            {
                "name": row["name"],
                "group": row["group"],
                "description": row["description"],
                "parameter_raw": parameter_raw,
                "returns_raw": row["returns_raw"],
                "param_names": param_names,
            },
        )

    payload: Dict[str, Any] = {
        "source_docx": str(docx_path),
        "tool_count": len(tools),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "groups": dict(Counter(tool["group"] for tool in tools)),
        "tools": tools,
    }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    root = Path(__file__).resolve().parent
    desktop = Path.home() / "Desktop"
    matches = sorted(desktop.glob("RenderDoc MCP*.docx"))
    if not matches:
        print("[spec] Missing source docx: ~/Desktop/RenderDoc MCP*.docx")
        return 1
    docx = matches[0]
    output = root / "tool_catalog_196.json"

    payload = build_catalog(docx, output)
    count = int(payload["tool_count"])
    print(f"[spec] Built catalog: {count} tools -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
