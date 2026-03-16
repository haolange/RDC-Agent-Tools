from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTEST_OUT = ROOT / "intermediate" / "pytest"
PYTEST_OUT.mkdir(parents=True, exist_ok=True)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rdx.context_snapshot import clear_context_snapshot
from rdx.runtime_paths import cli_runtime_dir
from rdx.runtime_state import clear_context_state

os.environ.setdefault("RDX_TOOLS_ROOT", str(ROOT))
os.environ.setdefault("RDX_ARTIFACT_DIR", str(ROOT / "intermediate" / "artifacts"))
os.environ.setdefault("RDX_RENDERDOC_PATH", str(ROOT / "binaries" / "windows" / "x64" / "pymodules"))
os.environ.setdefault("RDX_RUNTIME_DLL_DIR", str(ROOT / "binaries" / "windows" / "x64"))


@pytest.fixture(autouse=True)
def _isolate_runtime_state() -> None:
    state_dir = cli_runtime_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("runtime_state*.json", "runtime_logs*.jsonl", "context_snapshot*.json"):
        for path in state_dir.glob(pattern):
            path.unlink(missing_ok=True)
    clear_context_state("default")
    clear_context_snapshot("default")
    try:
        yield
    finally:
        for pattern in ("runtime_state*.json", "runtime_logs*.jsonl", "context_snapshot*.json"):
            for path in state_dir.glob(pattern):
                path.unlink(missing_ok=True)
        clear_context_state("default")
        clear_context_snapshot("default")
