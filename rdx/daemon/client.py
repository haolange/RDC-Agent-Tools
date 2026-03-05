"""Client utilities for RDX daemon over Windows named pipes."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from multiprocessing.connection import Client

from rdx.runtime_paths import cli_runtime_dir

STATE_DIR = cli_runtime_dir()
DAEMON_STATE_FILE = STATE_DIR / "daemon_state.json"
SESSION_STATE_FILE = STATE_DIR / "session_state.json"


def _normalize_context(context: Optional[str]) -> str:
    ctx = str(context or "").strip()
    return ctx if ctx and ctx.lower() != "default" else "default"


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
        tools_root_path = str(Path(tools_root).resolve())
        py_path = env.get("PYTHONPATH", "")
        if py_path:
            env["PYTHONPATH"] = tools_root_path + os.pathsep + py_path
        else:
            env["PYTHONPATH"] = tools_root_path
    popen_kwargs["env"] = env
    # Start daemon in background without opening a new console window.
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
    # Wait for ping.
    for _ in range(80):
        try:
            resp = daemon_request("ping", params={}, timeout=0.5, state=state)
            if bool(resp.get("ok")):
                save_daemon_state(state, context=chosen_ctx)
                return True, f"daemon started ({chosen_pipe})", state
        except Exception:
            time.sleep(0.1)
            continue
    try:
        proc.kill()
    except Exception:
        pass
    return False, "daemon failed to start", {}


def stop_daemon(context: Optional[str] = "default") -> Tuple[bool, str]:
    chosen_ctx = _normalize_context(context)
    st = load_daemon_state(context=chosen_ctx)
    if not st:
        return False, "no active daemon"
    try:
        daemon_request("shutdown", params={}, context=chosen_ctx)
    except Exception:
        pass
    clear_daemon_state(context=chosen_ctx)
    return True, "daemon stopped"
