from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_catalog_has_unique_tools_and_declared_count() -> None:
    catalog = ROOT / "spec" / "tool_catalog_196.json"
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    tools = payload.get("tools", [])
    names = [str(t.get("name", "")).strip() for t in tools]
    declared_count = int(payload.get("tool_count") or len(names))
    assert len(names) == declared_count
    assert len(set(names)) == len(names)
    assert all(n.startswith("rd.") for n in names)


def test_required_directories_exist() -> None:
    required = [
        ROOT / "rdx",
        ROOT / "mcp",
        ROOT / "cli",
        ROOT / "spec",
        ROOT / "policy",
        ROOT / "docs",
        ROOT / "tests",
        ROOT / "binaries" / "windows" / "x64" / "pymodules",
        ROOT / "intermediate" / "runtime" / "rdx_cli",
        ROOT / "intermediate" / "artifacts",
        ROOT / "intermediate" / "pytest",
        ROOT / "intermediate" / "logs",
    ]
    for p in required:
        assert p.is_dir(), str(p)
