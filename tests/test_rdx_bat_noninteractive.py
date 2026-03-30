from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    assert start >= 0 and end > start, text
    return json.loads(text[start : end + 1])


def _cmd_exe() -> str:
    system_root = str(os.environ.get("SystemRoot") or r"C:\Windows")
    return str(Path(system_root) / "System32" / "cmd.exe")


def _launcher_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("RDX_TOOLS_ROOT", str(ROOT))
    env.pop("RDX_PYTHON", None)
    return env


def _run_bat(*args: str) -> tuple[int, dict, str]:
    proc = subprocess.run(
        [_cmd_exe(), "/c", "rdx.bat", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        env=_launcher_env(),
        check=False,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, _extract_json(combined), combined


def _cleanup_context(context_id: str) -> None:
    for command in (("daemon", "stop"), ("context", "clear")):
        subprocess.run(
            [sys.executable, "cli/run_cli.py", "--daemon-context", context_id, *command],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=_launcher_env(),
            check=False,
        )


@pytest.mark.skipif(os.name != "nt", reason="rdx.bat launcher tests are windows-specific")
def test_noninteractive_daemon_status_returns_full_payload() -> None:
    context_id = "pytest-bat-daemon-status"
    try:
        code, payload, _ = _run_bat("--non-interactive", "cli", "--daemon-context", context_id, "daemon", "status")
    finally:
        _cleanup_context(context_id)

    assert code == 0
    assert payload["ok"] is True
    assert payload["result_kind"] == "rdx.daemon.status"
    assert isinstance(payload.get("data"), dict)
    assert isinstance(payload["data"].get("state"), dict)


@pytest.mark.skipif(os.name != "nt", reason="rdx.bat launcher tests are windows-specific")
def test_noninteractive_capture_status_returns_full_payload() -> None:
    context_id = "pytest-bat-capture-status"
    try:
        code, payload, _ = _run_bat("--non-interactive", "cli", "--daemon-context", context_id, "capture", "status")
    finally:
        _cleanup_context(context_id)

    assert code == 0
    assert payload["ok"] is True
    assert payload["result_kind"] == "rdx.capture.status"
    assert isinstance(payload.get("data"), dict)
    assert payload["data"]["context_id"] == context_id
    assert isinstance(payload["data"].get("context"), dict)


@pytest.mark.skipif(os.name != "nt", reason="rdx.bat launcher tests are windows-specific")
def test_noninteractive_mcp_ensure_env_reports_bundled_python() -> None:
    code, payload, _ = _run_bat("--non-interactive", "mcp", "--ensure-env")

    assert code == 0
    assert payload["ok"] is True
    details = payload["details"]
    python_details = details["python"]
    bundled = python_details["bundled_python"]
    assert bundled["python_entry"].endswith("python.exe")
    assert bundled["python_version"]


@pytest.mark.skipif(os.name != "nt", reason="rdx.bat launcher tests are windows-specific")
def test_noninteractive_launcher_errors_keep_short_status_payload() -> None:
    code, payload, output = _run_bat("--non-interactive", "unknown-command")

    assert code == 1
    assert payload["ok"] is False
    assert payload["error_code"] == 1
    assert "result_kind" not in payload
    assert "unknown command" in output