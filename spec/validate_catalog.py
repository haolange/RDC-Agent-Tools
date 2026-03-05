from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    catalog_path = root / "tool_catalog_196.json"
    if not catalog_path.is_file():
        print(f"[spec] Missing catalog: {catalog_path}")
        return 1

    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    tools = data.get("tools", [])
    names = [str(item.get("name", "")).strip() for item in tools]
    unique = set(names)

    if len(tools) != 196:
        print(f"[spec] Expected 196 tools, got {len(tools)}")
        return 2
    if len(unique) != 196:
        print(
            f"[spec] Tool names must be unique: {len(unique)} unique / {len(names)} total",
        )
        return 3
    if any(not name.startswith("rd.") for name in unique):
        print("[spec] Invalid tool name prefix found (must start with rd.)")
        return 4

    print("[spec] Catalog validation passed (196 unique rd.* tools)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
