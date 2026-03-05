"""Runtime path helpers for the standalone rdx-tools distribution."""

from __future__ import annotations

import os
from pathlib import Path


def tools_root() -> Path:
    """Return rdx-tools root directory."""
    env = os.environ.get("RDX_TOOLS_ROOT", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    # <root>/rdx/runtime_paths.py
    return Path(__file__).resolve().parents[1]


def binaries_root() -> Path:
    return tools_root() / "binaries" / "windows" / "x64"


def pymodules_dir() -> Path:
    return binaries_root() / "pymodules"


def intermediate_root() -> Path:
    return tools_root() / "intermediate"


def runtime_root() -> Path:
    return intermediate_root() / "runtime"


def cli_runtime_dir() -> Path:
    return runtime_root() / "rdx_cli"


def artifacts_dir() -> Path:
    return intermediate_root() / "artifacts"


def pytest_dir() -> Path:
    return intermediate_root() / "pytest"


def logs_dir() -> Path:
    return intermediate_root() / "logs"


def ensure_runtime_dirs() -> None:
    for path in (
        intermediate_root(),
        runtime_root(),
        cli_runtime_dir(),
        artifacts_dir(),
        pytest_dir(),
        logs_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
