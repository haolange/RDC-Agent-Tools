"""Runtime path helpers for the standalone rdx-tools distribution."""

from __future__ import annotations

import os
import sys
from pathlib import Path


_RDX_TOOLS_ROOT_ENV = "RDX_TOOLS_ROOT"
_WARNED_ROOT_OVERRIDE = False


def tools_root() -> Path:
    """Return rdx-tools root directory with env-first resolution.

    Resolution strategy (in priority order):
    1) ``RDX_TOOLS_ROOT`` if set and valid.
    2) Directory containing this file's package root (``rdx/`` parent).

    If both are set and different, a single warning is emitted and env takes
    precedence.
    """

    global _WARNED_ROOT_OVERRIDE
    fallback = Path(__file__).resolve().parents[1]

    env_root = os.environ.get(_RDX_TOOLS_ROOT_ENV, "").strip()
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        if candidate.is_dir():
            if not _WARNED_ROOT_OVERRIDE and candidate != fallback:
                print(
                    f"[rdx] warning: {_RDX_TOOLS_ROOT_ENV} overrides script root, "
                    f"using {candidate} (script root: {fallback})",
                    file=sys.stderr,
                )
                _WARNED_ROOT_OVERRIDE = True
            return candidate

    return fallback


def require_tools_root() -> Path:
    """Return tools root and raise when invalid."""
    root = tools_root()
    if not root.is_dir():
        raise RuntimeError(f"rdx-tools root not found: {root}")
    return root


def ensure_tools_root_env(*, force: bool = False) -> Path:
    """Ensure ``RDX_TOOLS_ROOT`` is set to the resolved root."""
    root = tools_root()
    if force or not os.environ.get(_RDX_TOOLS_ROOT_ENV):
        os.environ[_RDX_TOOLS_ROOT_ENV] = str(root)
    return root


def binaries_root() -> Path:
    """Directory containing ``renderdoc.dll`` and third-party runtime artifacts."""
    return tools_root() / "binaries" / "windows" / "x64"


def bundled_python_root() -> Path:
    """Directory containing the bundled CPython runtime for Windows users."""
    return binaries_root() / "python"


def bundled_python_executable() -> Path:
    return bundled_python_root() / "python.exe"


def android_binaries_root() -> Path:
    """Directory containing packaged Android `RenderDocCmd` APKs."""
    return tools_root() / "binaries" / "android"


def pymodules_dir() -> Path:
    return binaries_root() / "pymodules"


def intermediate_root() -> Path:
    return tools_root() / "intermediate"


def runtime_root() -> Path:
    return intermediate_root() / "runtime"


def cli_runtime_dir() -> Path:
    return runtime_root() / "rdx_cli"


def worker_cache_dir() -> Path:
    return runtime_root() / "worker-cache"


def worker_state_dir() -> Path:
    return runtime_root() / "worker-state"


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
        worker_cache_dir(),
        worker_state_dir(),
        artifacts_dir(),
        pytest_dir(),
        logs_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)