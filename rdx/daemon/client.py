"""Client utilities for RDX daemon over Windows named pipes."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from rdx.runtime_paths import cli_runtime_dir

STATE_DIR = cli_runtime_dir()
DAEMON_STATE_FILE = STATE_DIR / "daemon_state.json"
SESSION_STATE_FILE = STATE_DIR / "session_state.json"
POLL_DELAYS = (0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 1.6, 2.0)
MAX_STOP_TIMEOUT_S = 8.0


def _normalize_context(context: Optional[str]) -> str:
    ctx = str(context or "").strip()
    if not ctx or ctx.lower() == "default":
        return "default"
    return ctx


def _daemon_state_path(context: Optional[str]) -> Path:
    ctx = _normalize_context(context)
    if ctx == "default":
        return DAEMON_STATE_FILE
    safe_ctx = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in ctx)
    return STATE_DIR / f"daemon_state_{safe_ctx}.json"


def _pipe_address(pipe_name: str) -> str:
    return rf"\\.\pipe\{pipe_name}"


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_state_dir()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_daemon_state(context: Optional[str] = "default") -> Dict[str, Any]:
    return _load_json(_daemon_state_path(context))


def save_daemon_state(payload: Dict[str, Any], context: Optional[str] = "default") -> None:
    _save_json(_daemon_state_path(context), payload)


def clear_daemon_state(context: Optional[str] = "default") -> None:
    try:
        _daemon_state_path(context).unlink(missing_ok=True)
    except Exception:
        pass


def load_session_state() -> Dict[str, Any]:
    return _load_json(SESSION_STATE_FILE)


def save_session_state(payload: Dict[str, Any]) -> None:
    _save_json(SESSION_STATE_FILE, payload)


def _is_windows_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        if not ok:
            return True
        return bool(code.value == 259)
    except Exception:
        return False


def _kill_process(pid: int) -> bool:
    try:
        proc = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/F", "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _is_process_running(pid: int) -> bool:
    if os.name == "nt":
        return _is_windows_process_running(pid)
    return False


def daemon_request(
    method: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
    state: Optional[Dict[str, Any]] = None,
    context: Optional[str] = "default",
) -> Dict[str, Any]:
    st = state or load_daemon_state(context=context)
    pipe_name = str(st.get("pipe_name") or "").strip()
    token = str(st.get("token") or "").strip()
    if not pipe_name or not token:
        raise RuntimeError("No active daemon state. Run `rdx daemon start` first.")
    address = _pipe_address(pipe_name)
    payload = {"token": token, "method": str(method), "params": dict(params or {})}
    conn = Client(address=address, family="AF_PIPE")
    try:
        conn.send(payload)
        if timeout:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if conn.poll(0.1):
                    break
            else:
                raise TimeoutError(f"Timed out waiting for daemon response to {method}")
        response = conn.recv()
    finally:
        conn.close()
    if not isinstance(response, dict):
        raise RuntimeError(f"Invalid daemon response: {type(response).__name__}")
    return response


def _wait_for_daemon_ready(state: Dict[str, Any]) -> bool:
    for delay in POLL_DELAYS:
        try:
            daemon_request("ping", params={}, timeout=1.0, state=state)
            return True
        except Exception:
            time.sleep(delay)
    return False


def _wait_for_pid_exit(pid: int, timeout_s: float) -> bool:
    if pid <= 0:
        return True
    deadline = time.time() + max(0.0, timeout_s)
    while time.time() < deadline:
        if not _is_process_running(pid):
            return True
        time.sleep(0.2)
    return not _is_process_running(pid)


def start_daemon(
    *,
    pipe_name: Optional[str] = None,
    token: Optional[str] = None,
    context: Optional[str] = "default",
    owner_pid: Optional[int] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    chosen_ctx = _normalize_context(context)
    chosen_pipe = pipe_name or f"rdx-daemon-{os.getpid()}-{secrets.token_hex(4)}"
    chosen_token = token or secrets.token_hex(16)
    cmd = [
        sys.executable,
        "-m",
        "rdx.daemon.server",
        "--pipe-name",
        chosen_pipe,
        "--token",
        chosen_token,
        "--daemon-context",
        chosen_ctx,
    ]
    if owner_pid is not None:
        cmd.extend(["--owner-pid", str(int(owner_pid))])

    popen_kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    env = dict(os.environ)
    tools_root = str(env.get("RDX_TOOLS_ROOT", "")).strip()
    if tools_root:
        py_path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(Path(tools_root).resolve()) + os.pathsep + py_path if py_path else str(Path(tools_root).resolve())
    popen_kwargs["env"] = env
    if os.name == "nt":
        popen_kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(cmd, **popen_kwargs)
    state = {
        "pipe_name": chosen_pipe,
        "token": chosen_token,
        "pid": int(proc.pid),
        "started_at_ms": int(time.time() * 1000),
        "daemon_context": chosen_ctx,
        "owner_pid": int(owner_pid) if owner_pid else 0,
    }

    if not _wait_for_daemon_ready(state):
        try:
            if proc and proc.pid:
                _kill_process(int(proc.pid))
        finally:
            clear_daemon_state(context=chosen_ctx)
        return False, "daemon failed to start (ready timeout)", {}

    save_daemon_state(state, context=chosen_ctx)
    return True, f"daemon started ({chosen_pipe})", state


def _shutdown_stateful_daemon(pid: int | None, state: Dict[str, Any], context: str) -> None:
    if pid and _is_process_running(pid):
        for _ in range(4):
            try:
                daemon_request("shutdown", params={}, state=state, context=context)
            except Exception:
                pass
            if _wait_for_pid_exit(pid, 2.0):
                return
            time.sleep(0.5)

        _kill_process(pid)
        _wait_for_pid_exit(pid, 3.0)


def stop_daemon(context: Optional[str] = "default") -> Tuple[bool, str]:
    chosen_ctx = _normalize_context(context)
    st = load_daemon_state(context=chosen_ctx)
    if not st:
        clear_daemon_state(context=chosen_ctx)
        return False, "no active daemon"

    pid = int(st.get("pid") or 0)
    _shutdown_stateful_daemon(pid, st, chosen_ctx)

    if pid and _is_process_running(pid):
        return False, "daemon stop timed out"

    clear_daemon_state(context=chosen_ctx)
    return True, "daemon stopped"
