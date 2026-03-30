"""Shared Python/runtime dependency requirements for rdx-tools."""

from __future__ import annotations

import importlib.util

REQUIRED_DEPENDENCIES: list[tuple[str, str]] = [
    ("mcp", "mcp.server.fastmcp"),
    ("mcp", "mcp.server.transport_security"),
    ("pydantic", "pydantic"),
    ("numpy", "numpy"),
    ("Pillow", "PIL"),
    ("jinja2", "jinja2"),
    ("aiofiles", "aiofiles"),
]

# The bundled site-packages payload is runtime-focused. Keep test and local
# development helpers out of the distributed Python runtime.
EXCLUDED_BUNDLED_SITE_PACKAGE_PREFIXES: tuple[str, ...] = (
    "__pycache__",
    "__editable__",
    "_pytest",
    "_virtualenv",
    "pytest",
    "pytest-",
    "pluggy",
    "pluggy-",
    "iniconfig",
    "iniconfig-",
    "pygments",
    "pygments-",
    "pip",
    "pip-",
    "pyarrow",
    "setuptools",
    "setuptools-",
    "wheel",
    "wheel-",
    "adodbapi",
    "adodbapi-",
    "isapi",
    "pythonwin",
    "rdx_tools-",
)


def module_available(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except ModuleNotFoundError:
        return False


def missing_dependencies() -> list[str]:
    missing: list[str] = []
    for dist_name, import_name in REQUIRED_DEPENDENCIES:
        if (not module_available(import_name)) and dist_name not in missing:
            missing.append(dist_name)
    return missing


def should_bundle_site_package(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.endswith(".pyc") or lowered.endswith(".pth"):
        return False
    return not any(
        lowered == prefix
        or lowered.startswith(prefix + ".")
        or lowered.startswith(prefix + "-")
        for prefix in EXCLUDED_BUNDLED_SITE_PACKAGE_PREFIXES
    )