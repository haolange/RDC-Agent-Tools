"""Client utilities for RDX daemon over Windows named pipes."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from rdx.context_snapshot import clear_context_snapshot
from rdx.runtime_state import clear_context_state
from rdx.runtime_paths import cli_runtime_dir

STATE_DIR = cli_runtime_dir()
DAEMON_STATE_FILE = STATE_DIR / "daemon_state.json"
SESSION_STATE_FILE = STATE_DIR / "session_state.json"
POLL_DELAYS = (0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 1.6, 2.0)
MAX_STOP_TIMEOUT_S = 8.0
DEFAULT_LEASE_TIMEOUT_S = 120
DEFAULT_IDLE_TIMEOUT_S = 15 * 60


def _now_ms() -> int:
    return int(time.time() * 1000)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_to_ms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(float(value))
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _normalize_timeout(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _normalize_context(context: Optional[str]) -> str:
    ctx = str(context or "").strip()
    if not ctx or ctx.lower() == "default":
        return "default"
    return ctx


def _sanitize_context(context: Optional[str]) -> str:
    ctx = _normalize_context(context)
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in ctx)


def _context_from_state_path(path: Path) -> str:
    name = path.name
    if name == DAEMON_STATE_FILE.name:
        return "default"
    prefix = "daemon_state_"
    suffix = ".json"
    if name.startswith(prefix) and name.endswith(suffix):
        raw = name[len(prefix) : -len(suffix)]
        return raw or "default"
    return "default"


def _daemon_state_path(context: Optional[str]) -> Path:
    ctx = _normalize_context(context)
    if ctx == "default":
        return DAEMON_STATE_FILE
    return STATE_DIR / f"daemon_state_{_sanitize_context(ctx)}.json"


def _session_state_path(context: Optional[str]) -> Path:
    ctx = _normalize_context(context)
    if ctx == "default":
        return SESSION_STATE_FILE
    return STATE_DIR / f"session_state_{_sanitize_context(ctx)}.json"


def session_state_path(context: Optional[str] = "default") -> Path:
    return _session_state_path(context)


def _pipe_address(pipe_name: str) -> str:
    return rf"\\.\pipe\{pipe_name}"


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _load_json_with_status(path: Path) -> Tuple[str, Dict[str, Any]]:
    if not path.is_file():
        return "missing", {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "invalid", {}
    if not isinstance(payload, dict):
        return "invalid", {}
    return "ok", payload


def _load_json(path: Path) -> Dict[str, Any]:
    status, payload = _load_json_with_status(path)
    return payload if status == "ok" else {}


def _save_json(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_state_dir()
    tmp_path = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _normalize_attached_clients(value: Any) -> list[Dict[str, Any]]:
    items = value if isinstance(value, list) else []
    normalized: list[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        attached_at = str(item.get("attached_at") or _utc_now_iso())
        heartbeat_at = str(item.get("last_heartbeat_at") or attached_at)
        normalized.append(
            {
                "client_id": str(item.get("client_id") or "").strip(),
                "client_type": str(item.get("client_type") or "unknown").strip() or "unknown",
                "pid": int(item.get("pid") or 0),
                "attached_at": attached_at,
                "last_heartbeat_at": heartbeat_at,
                "lease_timeout_seconds": _normalize_timeout(
                    item.get("lease_timeout_seconds"),
                    default=DEFAULT_LEASE_TIMEOUT_S,
                ),
            }
        )
    return normalized


def _normalize_daemon_state_payload(payload: Dict[str, Any], context: Optional[str]) -> Dict[str, Any]:
    now_iso = _utc_now_iso()
    ctx = _normalize_context(context or payload.get("context_id") or payload.get("daemon_context"))
    started_at = str(payload.get("started_at") or payload.get("started_at_ms") or now_iso)
    last_activity_at = str(payload.get("last_activity_at") or payload.get("last_activity_at_ms") or started_at)
    state = dict(payload)
    state["context_id"] = ctx
    state["daemon_context"] = ctx
    state["pipe_name"] = str(payload.get("pipe_name") or "").strip()
    state["token"] = str(payload.get("token") or "").strip()
    state["pid"] = int(payload.get("pid") or 0)
    state["started_at"] = started_at
    state["last_activity_at"] = last_activity_at
    state["owner_pid"] = int(payload.get("owner_pid") or 0)
    state["lease_timeout_seconds"] = _normalize_timeout(
        payload.get("lease_timeout_seconds"),
        default=DEFAULT_LEASE_TIMEOUT_S,
    )
    state["idle_timeout_seconds"] = _normalize_timeout(
        payload.get("idle_timeout_seconds"),
        default=DEFAULT_IDLE_TIMEOUT_S,
    )
    state["attached_clients"] = _normalize_attached_clients(payload.get("attached_clients"))
    state["active_request_count"] = int(payload.get("active_request_count") or 0)
    active_operation = payload.get("active_operation")
    state["active_operation"] = dict(active_operation) if isinstance(active_operation, dict) else {}
    state["session_id"] = str(payload.get("session_id") or "").strip()
    state["capture_file_id"] = str(payload.get("capture_file_id") or "").strip()
    state["capture_path"] = str(payload.get("capture_path") or "").strip()
    state["active_event_id"] = int(payload.get("active_event_id") or 0)
    state["frame_index"] = int(payload.get("frame_index") or 0)
    state["session_count"] = int(payload.get("session_count") or 0)
    state["capture_count"] = int(payload.get("capture_count") or 0)
    state["recovery_status"] = str(payload.get("recovery_status") or "").strip()
    return state


def load_daemon_state(context: Optional[str] = "default") -> Dict[str, Any]:
    path = _daemon_state_path(context)
    payload = _load_json(path)
    if not payload:
        return {}
    return _normalize_daemon_state_payload(payload, context)


def save_daemon_state(payload: Dict[str, Any], context: Optional[str] = "default") -> None:
    _save_json(_daemon_state_path(context), _normalize_daemon_state_payload(payload, context))


def clear_daemon_state(context: Optional[str] = "default") -> None:
    try:
        _daemon_state_path(context).unlink(missing_ok=True)
    except Exception:
        pass


def load_session_state(context: Optional[str] = "default") -> Dict[str, Any]:
    ctx = _normalize_context(context)
    path = _session_state_path(ctx)
    payload = _load_json(path)
    if payload:
        return payload
    if ctx == "default" and path != SESSION_STATE_FILE:
        return _load_json(SESSION_STATE_FILE)
    return {}


def save_session_state(payload: Dict[str, Any], context: Optional[str] = "default") -> None:
    _save_json(_session_state_path(context), dict(payload or {}))


def clear_session_state(context: Optional[str] = "default") -> None:
    try:
        _session_state_path(context).unlink(missing_ok=True)
    except Exception:
        pass


def _state_paths(context: Optional[str] = None) -> Iterable[Path]:
    _ensure_state_dir()
    if context is not None:
        path = _daemon_state_path(context)
        if path.is_file():
            yield path
        return
    yield from STATE_DIR.glob("daemon_state*.json")


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


def _is_client_live(client: Dict[str, Any], now_ms: int) -> bool:
    pid = int(client.get("pid") or 0)
    if pid > 0 and not _is_process_running(pid):
        return False
    heartbeat_ms = _iso_to_ms(client.get("last_heartbeat_at"))
    lease_ms = _normalize_timeout(
        client.get("lease_timeout_seconds"),
        default=DEFAULT_LEASE_TIMEOUT_S,
    ) * 1000
    if heartbeat_ms <= 0:
        return True
    return (now_ms - heartbeat_ms) <= lease_ms


def _state_should_reap(state: Dict[str, Any], now_ms: Optional[int] = None) -> bool:
    if not state:
        return True
    current_ms = _now_ms() if now_ms is None else int(now_ms)
    pid = int(state.get("pid") or 0)
    if pid <= 0 or not _is_process_running(pid):
        return True

    clients = _normalize_attached_clients(state.get("attached_clients"))
    live_clients = [item for item in clients if _is_client_live(item, current_ms)]
    if live_clients:
        return False

    last_activity_ms = _iso_to_ms(state.get("last_activity_at"))
    if last_activity_ms <= 0:
        last_activity_ms = _iso_to_ms(state.get("started_at"))
    idle_timeout_ms = _normalize_timeout(
        state.get("idle_timeout_seconds"),
        default=DEFAULT_IDLE_TIMEOUT_S,
    ) * 1000
    if last_activity_ms > 0 and (current_ms - last_activity_ms) >= idle_timeout_ms:
        return True

    owner_pid = int(state.get("owner_pid") or 0)
    lease_timeout_ms = _normalize_timeout(
        state.get("lease_timeout_seconds"),
        default=DEFAULT_LEASE_TIMEOUT_S,
    ) * 1000
    if owner_pid > 0 and not _is_process_running(owner_pid):
        if last_activity_ms <= 0:
            return True
        return (current_ms - last_activity_ms) >= lease_timeout_ms

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
            daemon_request("ping", params={}, timeout=1.0, state=state, context=state.get("context_id"))
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


def cleanup_stale_daemon_states(context: Optional[str] = None) -> Dict[str, list[str]]:
    cleaned = {"state_files": [], "session_files": [], "killed_pids": []}
    for path in _state_paths(context):
        status, raw = _load_json_with_status(path)
        ctx = _context_from_state_path(path)
        if status == "missing":
            continue
        if status == "invalid":
            continue
        state = _normalize_daemon_state_payload(raw, ctx) if raw else {}
        pid = int(state.get("pid") or 0)

        if _state_should_reap(state):
            if pid > 0 and _is_process_running(pid):
                try:
                    daemon_request("shutdown", params={}, state=state, context=ctx)
                except Exception:
                    pass
                if not _wait_for_pid_exit(pid, MAX_STOP_TIMEOUT_S):
                    if _kill_process(pid):
                        cleaned["killed_pids"].append(str(pid))
                        _wait_for_pid_exit(pid, 3.0)
            clear_daemon_state(ctx)
            clear_session_state(ctx)
            cleaned["state_files"].append(path.name)
            cleaned["session_files"].append(_session_state_path(ctx).name)
    return cleaned


def _update_saved_state_from_response(response: Dict[str, Any], context: str) -> Dict[str, Any]:
    result = response.get("result")
    if isinstance(result, dict):
        daemon_state = result.get("state")
        if isinstance(daemon_state, dict):
            normalized = _normalize_daemon_state_payload(daemon_state, context)
            save_daemon_state(normalized, context=context)
            return normalized
    state = load_daemon_state(context=context)
    if state:
        return state
    return {}


def ensure_daemon(
    *,
    pipe_name: Optional[str] = None,
    token: Optional[str] = None,
    context: Optional[str] = "default",
    owner_pid: Optional[int] = None,
    lease_timeout_seconds: int = DEFAULT_LEASE_TIMEOUT_S,
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_S,
) -> Tuple[bool, str, Dict[str, Any]]:
    chosen_ctx = _normalize_context(context)
    cleanup_stale_daemon_states(context=chosen_ctx)

    existing = load_daemon_state(context=chosen_ctx)
    if existing:
        pid = int(existing.get("pid") or 0)
        if pid > 0 and _is_process_running(pid):
            try:
                response = daemon_request("status", params={}, context=chosen_ctx, state=existing)
            except Exception:
                _shutdown_stateful_daemon(pid, existing, chosen_ctx)
                clear_daemon_state(chosen_ctx)
                clear_session_state(chosen_ctx)
            else:
                updated = _update_saved_state_from_response(response, chosen_ctx)
                return True, "daemon already running", updated or existing
        else:
            clear_daemon_state(chosen_ctx)
            clear_session_state(chosen_ctx)

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
        "--lease-timeout-seconds",
        str(_normalize_timeout(lease_timeout_seconds, default=DEFAULT_LEASE_TIMEOUT_S)),
        "--idle-timeout-seconds",
        str(_normalize_timeout(idle_timeout_seconds, default=DEFAULT_IDLE_TIMEOUT_S)),
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
        root_path = str(Path(tools_root).resolve())
        env["PYTHONPATH"] = root_path + os.pathsep + py_path if py_path else root_path
    popen_kwargs["env"] = env
    if os.name == "nt":
        popen_kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(cmd, **popen_kwargs)
    state = _normalize_daemon_state_payload(
        {
            "pipe_name": chosen_pipe,
            "token": chosen_token,
            "pid": int(proc.pid),
            "started_at": _utc_now_iso(),
            "last_activity_at": _utc_now_iso(),
            "daemon_context": chosen_ctx,
            "context_id": chosen_ctx,
            "owner_pid": int(owner_pid) if owner_pid else 0,
            "lease_timeout_seconds": lease_timeout_seconds,
            "idle_timeout_seconds": idle_timeout_seconds,
            "attached_clients": [],
        },
        chosen_ctx,
    )

    if not _wait_for_daemon_ready(state):
        try:
            if proc and proc.pid:
                _kill_process(int(proc.pid))
        finally:
            clear_daemon_state(context=chosen_ctx)
            clear_session_state(context=chosen_ctx)
        return False, "daemon failed to start (ready timeout)", {}

    try:
        response = daemon_request("status", params={}, timeout=2.0, state=state, context=chosen_ctx)
    except Exception:
        save_daemon_state(state, context=chosen_ctx)
        return True, f"daemon started ({chosen_pipe})", state

    updated = _update_saved_state_from_response(response, chosen_ctx)
    return True, f"daemon started ({chosen_pipe})", updated or state


def start_daemon(
    *,
    pipe_name: Optional[str] = None,
    token: Optional[str] = None,
    context: Optional[str] = "default",
    owner_pid: Optional[int] = None,
    lease_timeout_seconds: int = DEFAULT_LEASE_TIMEOUT_S,
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_S,
) -> Tuple[bool, str, Dict[str, Any]]:
    return ensure_daemon(
        pipe_name=pipe_name,
        token=token,
        context=context,
        owner_pid=owner_pid,
        lease_timeout_seconds=lease_timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )


def attach_client(
    *,
    context: Optional[str] = "default",
    client_id: str,
    client_type: str,
    pid: int,
    lease_timeout_seconds: int = DEFAULT_LEASE_TIMEOUT_S,
) -> Tuple[bool, str, Dict[str, Any]]:
    chosen_ctx = _normalize_context(context)
    ok, message, state = ensure_daemon(context=chosen_ctx, lease_timeout_seconds=lease_timeout_seconds)
    if not ok:
        return False, message, {}
    response = daemon_request(
        "attach_client",
        params={
            "client_id": str(client_id),
            "client_type": str(client_type),
            "pid": int(pid),
            "lease_timeout_seconds": _normalize_timeout(
                lease_timeout_seconds,
                default=DEFAULT_LEASE_TIMEOUT_S,
            ),
        },
        context=chosen_ctx,
        state=state,
    )
    if not bool(response.get("ok")):
        err = response.get("error") if isinstance(response.get("error"), dict) else {}
        return False, str(err.get("message") or "attach client failed"), state
    updated = _update_saved_state_from_response(response, chosen_ctx)
    return True, message, updated or state


def heartbeat_client(
    *,
    context: Optional[str] = "default",
    client_id: str,
    pid: int = 0,
) -> Tuple[bool, str, Dict[str, Any]]:
    chosen_ctx = _normalize_context(context)
    state = load_daemon_state(context=chosen_ctx)
    if not state:
        return False, "no active daemon", {}
    response = daemon_request(
        "heartbeat",
        params={"client_id": str(client_id), "pid": int(pid or 0)},
        context=chosen_ctx,
        state=state,
    )
    if not bool(response.get("ok")):
        err = response.get("error") if isinstance(response.get("error"), dict) else {}
        return False, str(err.get("message") or "heartbeat failed"), state
    updated = _update_saved_state_from_response(response, chosen_ctx)
    return True, "heartbeat updated", updated or state


def detach_client(
    *,
    context: Optional[str] = "default",
    client_id: str,
) -> Tuple[bool, str, Dict[str, Any]]:
    chosen_ctx = _normalize_context(context)
    state = load_daemon_state(context=chosen_ctx)
    if not state:
        return False, "no active daemon", {}
    response = daemon_request(
        "detach_client",
        params={"client_id": str(client_id)},
        context=chosen_ctx,
        state=state,
    )
    if not bool(response.get("ok")):
        err = response.get("error") if isinstance(response.get("error"), dict) else {}
        return False, str(err.get("message") or "detach failed"), state
    updated = _update_saved_state_from_response(response, chosen_ctx)
    return True, "client detached", updated or state


def clear_context(context: Optional[str] = "default") -> Tuple[bool, str, Dict[str, Any]]:
    chosen_ctx = _normalize_context(context)
    state = load_daemon_state(context=chosen_ctx)
    if not state:
        cleanup_stale_daemon_states(context=chosen_ctx)
        clear_session_state(context=chosen_ctx)
        clear_context_snapshot(context=chosen_ctx)
        clear_context_state(context=chosen_ctx)
        return True, "context cleared (no active daemon)", {"released": {}, "state": {}}

    try:
        response = daemon_request("clear_context", params={}, context=chosen_ctx, state=state)
    except Exception:
        cleanup_stale_daemon_states(context=chosen_ctx)
        refreshed = load_daemon_state(context=chosen_ctx)
        if refreshed:
            return False, "context clear failed", {"state": refreshed}
        clear_session_state(context=chosen_ctx)
        clear_context_snapshot(context=chosen_ctx)
        clear_context_state(context=chosen_ctx)
        return True, "context cleared (no active daemon)", {"released": {}, "state": {}}
    if not bool(response.get("ok")):
        err = response.get("error") if isinstance(response.get("error"), dict) else {}
        return False, str(err.get("message") or "context clear failed"), {}
    clear_session_state(context=chosen_ctx)
    clear_context_snapshot(context=chosen_ctx)
    clear_context_state(context=chosen_ctx)
    updated = _update_saved_state_from_response(response, chosen_ctx)
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    details = dict(result)
    if updated:
        details["state"] = updated
    return True, "context cleared", details


def stop_daemon(context: Optional[str] = "default") -> Tuple[bool, str]:
    chosen_ctx = _normalize_context(context)
    st = load_daemon_state(context=chosen_ctx)
    if not st:
        cleanup_stale_daemon_states(context=chosen_ctx)
        st = load_daemon_state(context=chosen_ctx)
    if not st:
        clear_daemon_state(context=chosen_ctx)
        clear_session_state(context=chosen_ctx)
        return False, "no active daemon"

    pid = int(st.get("pid") or 0)
    _shutdown_stateful_daemon(pid, st, chosen_ctx)

    if pid and _is_process_running(pid):
        return False, "daemon stop timed out"

    clear_daemon_state(context=chosen_ctx)
    clear_session_state(context=chosen_ctx)
    return True, "daemon stopped"
