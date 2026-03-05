"""Deterministic TSV projection helpers."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .contracts import TSV_FORMAT_VERSION, normalize_drilldown_fields, stable_tsv_header


def _escape(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text.replace("\t", "\\t").replace("\n", "\\n")


def project_rows(
    rows: Sequence[Dict[str, Any]],
    *,
    columns: Iterable[str],
    format_version: str = TSV_FORMAT_VERSION,
) -> Tuple[List[str], List[List[str]]]:
    """Project row dicts into stable TSV header + rows."""
    header = stable_tsv_header(columns)
    body: List[List[str]] = []
    for row in rows:
        normalized = normalize_drilldown_fields(dict(row))
        cells: List[str] = []
        for col in header:
            if col == "format_version":
                cells.append(_escape(format_version))
            else:
                cells.append(_escape(normalized.get(col)))
        body.append(cells)
    return header, body


def to_tsv_string(
    rows: Sequence[Dict[str, Any]],
    *,
    columns: Iterable[str],
    format_version: str = TSV_FORMAT_VERSION,
    include_header: bool = True,
) -> str:
    header, body = project_rows(rows, columns=columns, format_version=format_version)
    lines: List[str] = []
    if include_header:
        lines.append("\t".join(header))
    for row in body:
        lines.append("\t".join(row))
    return "\n".join(lines)

