#!/usr/bin/env python3
"""Safe cleanup helper for rdx-tools temporary files."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


TARGET_NAMES = {".venv", ".pytest_cache", "__pycache__"}
TARGET_SUFFIXES = {".pyc"}
TARGET_GLOBS = ("*.egg-info",)


def _tools_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _collect_targets(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.name in TARGET_NAMES:
            out.append(p)
            continue
        if p.suffix.lower() in TARGET_SUFFIXES:
            out.append(p)
            continue
    for pattern in TARGET_GLOBS:
        for p in root.rglob(pattern):
            out.append(p)
    dedup = sorted({p.resolve() for p in out})
    safe: list[Path] = []
    root_resolved = root.resolve()
    for p in dedup:
        try:
            p.relative_to(root_resolved)
        except Exception:
            continue
        safe.append(p)
    return safe


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean temporary files inside rdx-tools")
    parser.add_argument("--apply", action="store_true", help="Delete after printing candidate list")
    args = parser.parse_args()

    root = _tools_root()
    targets = _collect_targets(root)
    print("待删除列表")
    for t in targets:
        print(str(t))
    if not args.apply:
        print("[cleanup] dry-run only (use --apply to delete)")
        return 0
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        else:
            try:
                t.unlink(missing_ok=True)
            except Exception:
                pass
    print(f"[cleanup] removed {len(targets)} paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
