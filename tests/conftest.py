from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTEST_OUT = ROOT / "intermediate" / "pytest"
PYTEST_OUT.mkdir(parents=True, exist_ok=True)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("RDX_TOOLS_ROOT", str(ROOT))
os.environ.setdefault("RDX_ARTIFACT_DIR", str(ROOT / "intermediate" / "artifacts"))
os.environ.setdefault("RDX_RENDERDOC_PATH", str(ROOT / "binaries" / "windows" / "x64" / "pymodules"))
os.environ.setdefault("RDX_RUNTIME_DLL_DIR", str(ROOT / "binaries" / "windows" / "x64"))
