from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from rdx.runtime_bootstrap import bootstrap_renderdoc_runtime
from rdx.runtime_paths import pymodules_dir


def test_bootstrap_honors_runtime_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = (tmp_path / "runtime").resolve()
    pymod_dir = (runtime_dir / "pymodules").resolve()
    pymod_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("RDX_RUNTIME_DLL_DIR", str(runtime_dir))
    monkeypatch.delenv("RDX_RENDERDOC_PATH", raising=False)

    report = bootstrap_renderdoc_runtime(probe_import=False)

    assert report.binaries_dir == runtime_dir
    assert report.pymodules_dir == pymod_dir
    assert str(pymod_dir) in sys.path
    assert os.environ.get("RDX_RUNTIME_DLL_DIR") == str(runtime_dir)
    assert os.environ.get("RDX_RENDERDOC_PATH") == str(pymod_dir)


@pytest.mark.skipif(os.name != "nt", reason="renderdoc runtime probing is windows-specific")
def test_bootstrap_probe_import_renderdoc() -> None:
    if not (pymodules_dir() / "renderdoc.pyd").is_file():
        pytest.skip("renderdoc runtime missing in binaries/windows/x64/pymodules")

    report = bootstrap_renderdoc_runtime(probe_import=True)

    assert report.import_ok, report.import_error
    assert "renderdoc.pyd" in report.import_module_path.lower()
