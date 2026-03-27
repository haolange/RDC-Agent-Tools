"""RDX daemon server using Windows named pipes."""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any, Dict

from rdx.context_snapshot import clear_context_snapshot
from rdx.daemon.client import (
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_LEASE_TIMEOUT_S,
    save_daemon_state,
)
from rdx.daemon.worker import RuntimeWorkerProcess
from rdx.progress import ProgressEvent, ProgressSink
from rdx.runtime_paths import cli_runtime_dir
from rdx.runtime_state import clear_context_state, load_context_state
from rdx.runtime_worker_state import load_worker_state
from rdx.timeout_policy import worker_exec_timeout_s

logger = logging.getLogger("rdx.daemon")


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


def _normalize_context(context: str) -> str:
    ctx = str(context or "default").strip()
    return ctx if ctx and ctx.lower() != "default" else "default"


def _daemon_state_path(context: str) -> Path:
    ctx = _normalize_context(context)
    state_dir = cli_runtime_dir()
    if ctx == "default":
        return state_dir / "daemon_state.json"
    safe_ctx = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in ctx)
    return state_dir / f"daemon_state_{safe_ctx}.json"


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        return True
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return bool(code.value == 259)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


class DaemonRuntime(ProgressSink):
    def __init__(
        self,
        *,
        pipe_name: str,
        token: str,
        daemon_context: str = "default",
        owner_pid: int = 0,
        lease_timeout_seconds: int = DEFAULT_LEASE_TIMEOUT_S,
        idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_S,
    ) -> None:
        self.pipe_name = pipe_name
        self.token = token
        self.address = rf"\\.\pipe\{pipe_name}"
        self.daemon_context = _normalize_context(daemon_context)
        self.owner_pid = int(owner_pid) if owner_pid else 0
        self.lease_timeout_seconds = _normalize_timeout(
            lease_timeout_seconds,
            default=DEFAULT_LEASE_TIMEOUT_S,
        )
        self.idle_timeout_seconds = _normalize_timeout(
            idle_timeout_seconds,
            default=DEFAULT_IDLE_TIMEOUT_S,
        )
        self.running = True
        now_iso = _utc_now_iso()
        self.state: Dict[str, Any] = {
            "context_id": self.daemon_context,
            "daemon_context": self.daemon_context,
            "pipe_name": pipe_name,
            "token": token,
            "pid": int(os.getpid()),
            "started_at": now_iso,
            "last_activity_at": now_iso,
            "owner_pid": self.owner_pid,
            "lease_timeout_seconds": self.lease_timeout_seconds,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "attached_clients": [],
            "active_request_count": 0,
            "active_operation": {},
            "session_id": "",
            "capture_file_id": "",
            "capture_path": "",
            "active_event_id": 0,
            "frame_index": 0,
            "session_count": 0,
            "capture_count": 0,
            "recovery_status": "idle",
            "backend": "local",
            "runtime_owner": {
                "agent_id": "",
                "lease_id": "",
                "status": "unclaimed",
                "claimed_at_ms": 0,
                "released_at_ms": 0,
            },
            "owner_lease": {
                "agent_id": "",
                "lease_id": "",
                "status": "unclaimed",
                "claimed_at_ms": 0,
                "released_at_ms": 0,
            },
            "worker": {
                "running": False,
                "pid": 0,
                "runtime_id": "",
                "cache_root": "",
                "source_manifest": "",
            },
        }
        self._listener = None
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._exec_lock = threading.Lock()
        self._worker: RuntimeWorkerProcess | None = None

    def _auth(self, request: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
        if str(request.get("token", "")) != self.token:
            return False, {"ok": False, "error": {"code": "unauthorized", "message": "invalid daemon token"}}
        return True, {}

    def _persist_state(self) -> None:
        with self._state_lock:
            payload = dict(self.state)
            payload["attached_clients"] = [dict(item) for item in payload.get("attached_clients", [])]
        save_daemon_state(payload, context=self.daemon_context)

    def _touch_activity(self) -> None:
        with self._state_lock:
            self.state["last_activity_at"] = _utc_now_iso()

    def _prune_clients_locked(self, now_ms: int) -> None:
        current = self.state.get("attached_clients")
        items = current if isinstance(current, list) else []
        kept: list[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = int(item.get("pid") or 0)
            if pid > 0 and not _is_process_running(pid):
                continue
            lease_ms = _normalize_timeout(
                item.get("lease_timeout_seconds"),
                default=self.lease_timeout_seconds,
            ) * 1000
            last_heartbeat_ms = _iso_to_ms(item.get("last_heartbeat_at"))
            if last_heartbeat_ms > 0 and (now_ms - last_heartbeat_ms) > lease_ms:
                continue
            kept.append(
                {
                    "client_id": str(item.get("client_id") or "").strip(),
                    "client_type": str(item.get("client_type") or "unknown").strip() or "unknown",
                    "pid": pid,
                    "attached_at": str(item.get("attached_at") or _utc_now_iso()),
                    "last_heartbeat_at": str(item.get("last_heartbeat_at") or _utc_now_iso()),
                    "lease_timeout_seconds": _normalize_timeout(
                        item.get("lease_timeout_seconds"),
                        default=self.lease_timeout_seconds,
                    ),
                }
            )
        self.state["attached_clients"] = kept

    def _snapshot_state(self) -> Dict[str, Any]:
        with self._state_lock:
            self._prune_clients_locked(_now_ms())
            self._refresh_state_from_runtime_locked()
            payload = dict(self.state)
            payload["attached_clients"] = [dict(item) for item in payload.get("attached_clients", [])]
            payload["worker"] = dict(payload.get("worker") or {})
        return payload

    def _status_payload(self) -> Dict[str, Any]:
        state = self._snapshot_state()
        self._persist_state()
        return {"ok": True, "result": {"running": self.running, "state": state}}

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        self.running = False
        self._stop_event.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass

    def _clear_context_snapshot_locked(self) -> None:
        self.state["session_id"] = ""
        self.state["capture_file_id"] = ""
        self.state["capture_path"] = ""
        self.state["active_event_id"] = 0
        self.state["frame_index"] = 0
        self.state["session_count"] = 0
        self.state["capture_count"] = 0
        self.state["recovery_status"] = "idle"
        self.state["backend"] = "local"
        self.state["runtime_owner"] = {
            "agent_id": "",
            "lease_id": "",
            "status": "unclaimed",
            "claimed_at_ms": 0,
            "released_at_ms": 0,
        }
        self.state["owner_lease"] = {
            "agent_id": "",
            "lease_id": "",
            "status": "unclaimed",
            "claimed_at_ms": 0,
            "released_at_ms": 0,
        }
        self.state["active_operation"] = {}

    def _set_active_operation_locked(
        self,
        *,
        operation: str,
        stage: str,
        message: str,
        trace_id: str = "",
        progress_pct: float | None = None,
        details: Dict[str, Any] | None = None,
    ) -> None:
        existing = self.state.get("active_operation")
        started_at_ms = 0
        if isinstance(existing, dict):
            started_at_ms = int(existing.get("started_at_ms") or 0)
        self.state["active_operation"] = {
            "trace_id": str(trace_id or (existing.get("trace_id") if isinstance(existing, dict) else "") or ""),
            "operation": str(operation),
            "stage": str(stage),
            "message": str(message),
            "progress_pct": progress_pct,
            "details": dict(details or {}),
            "started_at_ms": started_at_ms or _now_ms(),
            "updated_at_ms": _now_ms(),
        }

    def _clear_active_operation_locked(self) -> None:
        self.state["active_operation"] = {}

    def publish(self, event: ProgressEvent) -> None:
        with self._state_lock:
            self._set_active_operation_locked(
                operation=event.operation,
                stage=event.stage,
                message=event.message,
                trace_id=event.trace_id,
                progress_pct=event.progress_pct,
                details=event.details,
            )
            self.state["last_activity_at"] = _utc_now_iso()
        self._persist_state()

    def _sync_context_snapshot_locked(self, params: Dict[str, Any]) -> None:
        mapping = {
            "session_id": "",
            "capture_file_id": "",
            "capture_path": "",
            "active_event_id": 0,
            "frame_index": 0,
        }
        for key, default in mapping.items():
            if key not in params:
                continue
            value = params.get(key, default)
            if isinstance(default, int):
                self.state[key] = int(value or 0)
            else:
                self.state[key] = str(value or "")

    def _worker_snapshot_locked(self) -> Dict[str, Any]:
        if self._worker is not None:
            worker = dict(self._worker.snapshot())
        else:
            worker = dict(load_worker_state(self.daemon_context) or {})
            pid = int(worker.get("pid") or 0)
            worker["running"] = bool(worker.get("running")) and _is_process_running(pid)
            if not worker["running"]:
                worker["pid"] = 0
        return {
            "running": bool(worker.get("running")),
            "pid": int(worker.get("pid") or 0),
            "runtime_id": str(worker.get("runtime_id") or ""),
            "cache_root": str(worker.get("cache_root") or ""),
            "source_manifest": str(worker.get("source_manifest") or ""),
        }

    def _refresh_state_from_runtime_locked(self) -> None:
        context_state = load_context_state(self.daemon_context)
        sessions = context_state.get("sessions", {}) if isinstance(context_state.get("sessions"), dict) else {}
        captures = context_state.get("captures", {}) if isinstance(context_state.get("captures"), dict) else {}
        current_session_id = str(context_state.get("current_session_id") or "")
        current_capture_file_id = str(context_state.get("current_capture_file_id") or "")
        current_session = sessions.get(current_session_id) if current_session_id else {}
        current_capture = captures.get(current_capture_file_id) if current_capture_file_id else {}

        self.state["session_id"] = current_session_id
        self.state["capture_file_id"] = current_capture_file_id
        self.state["capture_path"] = str(
            (current_capture or {}).get("file_path")
            or (current_session or {}).get("rdc_path")
            or ""
        )
        self.state["active_event_id"] = int((current_session or {}).get("active_event_id") or 0)
        self.state["frame_index"] = int((current_session or {}).get("frame_index") or 0)
        self.state["session_count"] = len(context_state.get("sessions", {}))
        self.state["capture_count"] = len(context_state.get("captures", {}))
        self.state["recovery_status"] = str((context_state.get("recovery") or {}).get("status") or "idle")
        self.state["backend"] = str(context_state.get("backend") or "local")
        self.state["runtime_owner"] = dict(context_state.get("runtime_owner") or {})
        self.state["owner_lease"] = dict(context_state.get("owner_lease") or {})
        self.state["worker"] = self._worker_snapshot_locked()

    def _ensure_worker(self) -> RuntimeWorkerProcess:
        if self._worker is None:
            self._worker = RuntimeWorkerProcess(context_id=self.daemon_context)
        self._worker.ensure_started()
        return self._worker

    def _request_worker(self, method: str, params: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        worker = self._ensure_worker()
        try:
            response = worker.request(method, params, timeout=timeout)
        except Exception:
            worker.stop()
            response = worker.request(method, params, timeout=timeout)
        if not bool(response.get("ok")):
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            raise RuntimeError(str(error.get("message") or f"worker {method} failed"))
        return dict(response)

    def _handle_attach_client(self, params: Dict[str, Any]) -> Dict[str, Any]:
        client_id = str(params.get("client_id") or "").strip()
        if not client_id:
            return {"ok": False, "error": {"code": "bad_request", "message": "client_id is required"}}
        now_iso = _utc_now_iso()
        client = {
            "client_id": client_id,
            "client_type": str(params.get("client_type") or "unknown").strip() or "unknown",
            "pid": int(params.get("pid") or 0),
            "attached_at": now_iso,
            "last_heartbeat_at": now_iso,
            "lease_timeout_seconds": _normalize_timeout(
                params.get("lease_timeout_seconds"),
                default=self.lease_timeout_seconds,
            ),
        }
        with self._state_lock:
            self._prune_clients_locked(_now_ms())
            clients = [item for item in self.state.get("attached_clients", []) if item.get("client_id") != client_id]
            clients.append(client)
            self.state["attached_clients"] = clients
            self.state["last_activity_at"] = now_iso
        self._persist_state()
        return self._status_payload()

    def _handle_heartbeat(self, params: Dict[str, Any]) -> Dict[str, Any]:
        client_id = str(params.get("client_id") or "").strip()
        if not client_id:
            return {"ok": False, "error": {"code": "bad_request", "message": "client_id is required"}}
        now_iso = _utc_now_iso()
        found = False
        with self._state_lock:
            self._prune_clients_locked(_now_ms())
            clients = []
            for item in self.state.get("attached_clients", []):
                if item.get("client_id") == client_id:
                    updated = dict(item)
                    if int(params.get("pid") or 0):
                        updated["pid"] = int(params.get("pid") or 0)
                    updated["last_heartbeat_at"] = now_iso
                    clients.append(updated)
                    found = True
                else:
                    clients.append(item)
            self.state["attached_clients"] = clients
            self.state["last_activity_at"] = now_iso
        if not found:
            return {"ok": False, "error": {"code": "not_found", "message": f"client not attached: {client_id}"}}
        self._persist_state()
        return self._status_payload()

    def _handle_detach_client(self, params: Dict[str, Any]) -> Dict[str, Any]:
        client_id = str(params.get("client_id") or "").strip()
        if not client_id:
            return {"ok": False, "error": {"code": "bad_request", "message": "client_id is required"}}
        with self._state_lock:
            clients = [item for item in self.state.get("attached_clients", []) if item.get("client_id") != client_id]
            self.state["attached_clients"] = clients
            self.state["last_activity_at"] = _utc_now_iso()
        self._persist_state()
        return self._status_payload()

    def _run_exec(self, operation: str, args: Dict[str, Any], *, transport: str, remote: bool) -> Dict[str, Any]:
        with self._exec_lock:
            with self._state_lock:
                self.state["active_request_count"] = int(self.state.get("active_request_count") or 0) + 1
                self.state["last_activity_at"] = _utc_now_iso()
                self._set_active_operation_locked(
                    operation=operation,
                    stage="starting",
                    message="Operation queued in daemon",
                )
            self._persist_state()
            try:
                worker_timeout_s = worker_exec_timeout_s(operation, args)
                response = self._request_worker(
                    "exec",
                    {
                        "operation": operation,
                        "args": dict(args or {}),
                        "transport": transport,
                        "remote": remote,
                        "context_id": self.daemon_context,
                    },
                    timeout=worker_timeout_s,
                )
                result = response.get("result")
                with self._state_lock:
                    self._refresh_state_from_runtime_locked()
                return {"ok": True, "result": result}
            except Exception as exc:
                with self._state_lock:
                    self._refresh_state_from_runtime_locked()
                return {"ok": False, "error": {"code": "worker_error", "message": str(exc)}}
            finally:
                with self._state_lock:
                    self.state["active_request_count"] = max(
                        0,
                        int(self.state.get("active_request_count") or 0) - 1,
                    )
                    self._clear_active_operation_locked()
                    self.state["last_activity_at"] = _utc_now_iso()
                self._persist_state()

    def _run_clear_context(self) -> Dict[str, Any]:
        with self._exec_lock:
            with self._state_lock:
                self.state["active_request_count"] = int(self.state.get("active_request_count") or 0) + 1
                self.state["last_activity_at"] = _utc_now_iso()
                self._set_active_operation_locked(
                    operation="rd.core.shutdown",
                    stage="starting",
                    message="Clearing daemon context",
                )
            self._persist_state()
            released: Dict[str, Any] = {}
            try:
                if self._worker is not None and self._worker.is_running():
                    response = self._request_worker("clear_context", {"context_id": self.daemon_context}, timeout=15.0)
                    result = response.get("result")
                    if isinstance(result, dict):
                        data = result.get("data")
                        if isinstance(data, dict):
                            released = dict(data.get("released") or {})
                else:
                    clear_context_snapshot(self.daemon_context)
                    clear_context_state(self.daemon_context)
            except Exception as exc:
                return {"ok": False, "error": {"code": "worker_error", "message": str(exc)}}
            finally:
                with self._state_lock:
                    self._refresh_state_from_runtime_locked()
                    self._clear_context_snapshot_locked()
                    self.state["active_request_count"] = max(
                        0,
                        int(self.state.get("active_request_count") or 0) - 1,
                    )
                    self._clear_active_operation_locked()
                    self.state["last_activity_at"] = _utc_now_iso()
                self._persist_state()
            return {"ok": True, "result": {"released": released, "state": self._snapshot_state()}}

    def _watch_lifecycle(self) -> None:
        while self.running:
            if self._stop_event.wait(1.0):
                break
            now_ms = _now_ms()
            should_stop = False
            reason = ""
            with self._state_lock:
                self._prune_clients_locked(now_ms)
                self.state["pid"] = int(os.getpid())
                attached = list(self.state.get("attached_clients", []))
                active_request_count = int(self.state.get("active_request_count") or 0)
                last_activity_ms = _iso_to_ms(self.state.get("last_activity_at"))
                owner_pid = int(self.state.get("owner_pid") or 0)
                if owner_pid > 0 and not _is_process_running(owner_pid) and not attached and active_request_count == 0:
                    lease_ms = self.lease_timeout_seconds * 1000
                    if last_activity_ms <= 0 or (now_ms - last_activity_ms) >= lease_ms:
                        should_stop = True
                        reason = f"owner pid {owner_pid} lost and lease expired"
                if not should_stop and not attached and active_request_count == 0:
                    idle_ms = self.idle_timeout_seconds * 1000
                    if last_activity_ms > 0 and (now_ms - last_activity_ms) >= idle_ms:
                        should_stop = True
                        reason = f"idle timeout exceeded ({self.idle_timeout_seconds}s)"
            self._persist_state()
            if should_stop:
                logger.info("daemon context=%s stopping: %s", self.daemon_context, reason)
                self._stop()
                break

    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": {"code": "bad_request", "message": "request must be an object"}}
        authorized, auth_error = self._auth(request)
        if not authorized:
            return auth_error

        method = str(request.get("method") or "").strip().lower()
        logger.info("daemon.request method=%s", method)
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}

        if method == "ping":
            self._touch_activity()
            self._persist_state()
            return {"ok": True, "result": {"pong": True, "context_id": self.daemon_context}}
        if method == "status":
            self._touch_activity()
            return self._status_payload()
        if method == "shutdown":
            self._touch_activity()
            self._stop()
            return {"ok": True, "result": {"stopping": True, "state": self._snapshot_state()}}
        if method == "attach_client":
            return self._handle_attach_client(params)
        if method == "heartbeat":
            return self._handle_heartbeat(params)
        if method == "detach_client":
            return self._handle_detach_client(params)
        if method == "set_state":
            with self._state_lock:
                self._sync_context_snapshot_locked(dict(params))
                self.state["last_activity_at"] = _utc_now_iso()
            self._persist_state()
            return self._status_payload()
        if method == "get_state":
            self._touch_activity()
            self._persist_state()
            return {"ok": True, "result": {"state": self._snapshot_state()}}
        if method == "clear_context":
            return self._run_clear_context()
        if method == "exec":
            operation = str(params.get("operation") or "").strip()
            args = params.get("args")
            if not isinstance(args, dict):
                args = {}
            transport = str(params.get("transport") or "daemon")
            remote = bool(params.get("remote", False))
            arg_keys = ",".join(sorted(args.keys())) if args else "-"
            logger.info(
                "daemon.exec op=%s transport=%s remote=%s arg_keys=%s",
                operation,
                transport,
                remote,
                arg_keys,
            )
            result = self._run_exec(operation, args, transport=transport, remote=remote)
            payload = result.get("result")
            if isinstance(payload, dict):
                meta = payload.get("meta", {})
                logger.info(
                    "daemon.exec.done op=%s trace_id=%s ok=%s duration_ms=%s",
                    operation,
                    (meta.get("trace_id") if isinstance(meta, dict) else ""),
                    bool(payload.get("ok")),
                    (meta.get("duration_ms") if isinstance(meta, dict) else None),
                )
            return result
        return {"ok": False, "error": {"code": "unknown_method", "message": f"unknown method: {method}"}}

    def _serve_connection(self, conn: Any) -> None:
        try:
            request = conn.recv()
            conn.send(self.handle_request(request))
        except Exception as exc:  # noqa: BLE001
            try:
                conn.send({"ok": False, "error": {"code": "daemon_error", "message": str(exc)}})
            except Exception:
                pass
        finally:
            conn.close()

    def serve_forever(self) -> int:
        self._listener = Listener(address=self.address, family="AF_PIPE")
        logger.info("daemon listening on %s", self.address)
        self._persist_state()
        try:
            threading.Thread(
                target=self._watch_lifecycle,
                name="rdx-daemon-lifecycle-monitor",
                daemon=True,
            ).start()
            while self.running:
                try:
                    conn = self._listener.accept()
                except Exception:
                    break
                threading.Thread(
                    target=self._serve_connection,
                    args=(conn,),
                    name=f"rdx-daemon-conn-{_now_ms()}",
                    daemon=True,
                ).start()
        finally:
            if self._listener is not None:
                try:
                    self._listener.close()
                except Exception:
                    pass
            state_path = _daemon_state_path(self.daemon_context)
            try:
                state_path.unlink(missing_ok=True)
            except Exception:
                pass
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="RDX named-pipe daemon")
    parser.add_argument("--pipe-name", required=True, help="Named pipe suffix (without \\\\.\\pipe\\)")
    parser.add_argument("--token", required=True, help="Authentication token")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--daemon-context", default="default")
    parser.add_argument("--owner-pid", type=int, default=0)
    parser.add_argument("--lease-timeout-seconds", type=int, default=DEFAULT_LEASE_TIMEOUT_S)
    parser.add_argument("--idle-timeout-seconds", type=int, default=DEFAULT_IDLE_TIMEOUT_S)
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    daemon = DaemonRuntime(
        pipe_name=str(args.pipe_name),
        token=str(args.token),
        daemon_context=str(args.daemon_context),
        owner_pid=int(args.owner_pid),
        lease_timeout_seconds=int(args.lease_timeout_seconds),
        idle_timeout_seconds=int(args.idle_timeout_seconds),
    )

    def _stop(*_a: Any) -> None:
        daemon._stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    raise SystemExit(daemon.serve_forever())


if __name__ == "__main__":
    main()
