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
        stdin=subprocess.DEVNULL,
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


def _run_bat_from_cwd(cwd: Path, *args: str) -> tuple[int, dict, str]:
    proc = subprocess.run(
        [_cmd_exe(), "/c", str(ROOT / "rdx.bat"), *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
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
            stdin=subprocess.DEVNULL,
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
        code, payload, _ = _run_bat("--non-interactive", "--daemon-context", context_id, "daemon", "status")
    finally:
        _cleanup_context(context_id)

    assert code == 0
    assert payload["ok"] is True
    assert payload["result_kind"] == "rdx.daemon.status"
    assert isinstance(payload.get("data"), dict)
    assert isinstance(payload["data"].get("state"), dict)


@pytest.mark.skipif(os.name != "nt", reason="rdx.bat launcher tests are windows-specific")
def test_noninteractive_doctor_returns_cli_only_payload() -> None:
    code, payload, _ = _run_bat("--non-interactive", "--json", "doctor")

    assert code == 0
    assert payload["ok"] is True
    assert payload["result_kind"] == "rdx.doctor"
    assert payload["data"]["mcp"]["supported"] is False


@pytest.mark.skipif(os.name != "nt", reason="rdx.bat launcher tests are windows-specific")
def test_noninteractive_mcp_route_reports_unsupported_json() -> None:
    code, payload, _ = _run_bat("--non-interactive", "mcp", "--ensure-env")

    assert code != 0
    assert payload["ok"] is False
    assert payload["error_code"] == "unsupported_command"


@pytest.mark.skipif(os.name != "nt", reason="rdx.bat launcher tests are windows-specific")
def test_noninteractive_launcher_missing_command_keeps_short_status_payload() -> None:
    code, payload, output = _run_bat("--non-interactive")

    assert code == 2
    assert payload["ok"] is False
    assert payload["error_code"] == "missing_command"
    assert "result_kind" not in payload
    assert "missing command" in output


@pytest.mark.skipif(os.name != "nt", reason="rdx.bat launcher tests are windows-specific")
def test_noninteractive_version_and_completion_are_available() -> None:
    version_code, version_payload, _ = _run_bat("--non-interactive", "version", "--json")
    completion_proc = subprocess.run(
        [_cmd_exe(), "/c", "rdx.bat", "--non-interactive", "completion", "powershell"],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        env=_launcher_env(),
        check=False,
    )

    assert version_code == 0
    assert version_payload["ok"] is True
    assert version_payload["result_kind"] == "rdx.version"
    assert completion_proc.returncode == 0
    assert "Register-ArgumentCompleter" in completion_proc.stdout

@pytest.mark.skipif(os.name != "nt", reason="rdx.bat launcher tests are windows-specific")
def test_noninteractive_tools_list_passthroughs_canonical_payload() -> None:
    code, payload, _ = _run_bat(
        "--non-interactive",
        "tools",
        "list",
        "--json",
        "--limit",
        "3",
    )

    assert code == 0
    assert payload["ok"] is True
    assert payload["result_kind"] == "rdx.tools.list"
    assert payload["data"]["tool_count"] >= 1


@pytest.mark.skipif(os.name != "nt", reason="rdx.bat launcher tests are windows-specific")
def test_noninteractive_tools_search_runs_from_caller_cwd(tmp_path: Path) -> None:
    code, payload, _ = _run_bat_from_cwd(
        tmp_path,
        "--non-interactive",
        "tools",
        "search",
        "pipeline",
        "--json",
    )

    assert code == 0
    assert payload["ok"] is True
    assert payload["result_kind"] == "rdx.tools.search"
    assert payload["data"]["tool_count"] >= 1
