from __future__ import annotations

import json
from pathlib import Path

from rdx import python_runtime


def _write(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def test_validate_bundled_python_layout_reports_complete_bundle(tmp_path: Path, monkeypatch) -> None:
    bin_root = tmp_path / "binaries" / "windows" / "x64"
    _write(bin_root / "python" / "python.exe", b"exe")
    _write(bin_root / "python" / "pythonw.exe", b"exew")
    _write(bin_root / "python" / "python3.dll", b"dll3")
    _write(bin_root / "python" / "python314.dll", b"dll314")
    _write(bin_root / "python" / "python314._pth", ".\nLib\nLib/site-packages\nDLLs\nimport site\n")
    _write(bin_root / "python" / "Lib" / "os.py", "print('os')\n")
    _write(bin_root / "python" / "Lib" / "site.py", "print('site')\n")
    _write(bin_root / "python" / "Lib" / "encodings" / "__init__.py", "# encodings\n")
    _write(bin_root / "python" / "Lib" / "site-packages" / "mcp.py", "# mcp\n")
    _write(bin_root / "python" / "DLLs" / "_socket.pyd", b"socket")
    _write(
        bin_root / "manifest.runtime.json",
        json.dumps(
            {
                "file_count": 1,
                "files": [{"path": "renderdoc.dll", "size": 1, "sha256": "00", "worker_materialize": True}],
                "bundled_python": {
                    "python_version": "3.14.3",
                    "python_entry": "python/python.exe",
                    "pythonw_entry": "python/pythonw.exe",
                    "python3_dll": "python/python3.dll",
                    "python_dll": "python/python314.dll",
                    "stdlib_layout": "python/Lib",
                    "site_packages": "python/Lib/site-packages",
                    "dll_dir": "python/DLLs",
                    "pth_file": "python/python314._pth",
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
    )

    monkeypatch.setattr(python_runtime, "binaries_root", lambda: bin_root)

    ok, failures, details = python_runtime.validate_bundled_python_layout()

    assert ok, failures
    assert failures == []
    bundled = details["bundled_python"]
    assert bundled["python_version"] == "3.14.3"
    assert bundled["python_entry"].endswith("python.exe")