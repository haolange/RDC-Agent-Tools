#!/usr/bin/env python3
"""Generate SHA256 checksums for selected release assets."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate SHA256 checksums for release assets")
    parser.add_argument("paths", nargs="+", help="Files or directories to include")
    parser.add_argument("--out", default="intermediate/logs/release_checksums.sha256")
    args = parser.parse_args(argv)

    rows: list[tuple[str, str]] = []
    for raw in args.paths:
        path = Path(raw).resolve()
        if path.is_file():
            rows.append((_sha256(path), path.name))
            continue
        if path.is_dir():
            for child in sorted(p for p in path.rglob("*") if p.is_file()):
                rows.append((_sha256(child), str(child.relative_to(path.parent)).replace("\\", "/")))
            continue
        raise SystemExit(f"missing path: {path}")

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(f"{sha}  {name}\n" for sha, name in rows), encoding="utf-8")
    print(f"[checksums] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
