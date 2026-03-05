"""RDX daemon server using Windows named pipes."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import os
import signal
import sys
import threading
import time
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any, Dict

from rdx.runtime_bootstrap import bootstrap_renderdoc_runtime
from rdx.runtime_paths import cli_runtime_dir
from rdx.server import dispatch_operation, runtime_shutdown, runtime_startup

logger = logging.getLogger("rdx.daemon")


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


class DaemonRuntime:
    def __init__(self, *, pipe_name: str, token: str, daemon_context: str = "default", owner_pid: int = 0) -> None:
        self.pipe_name = pipe_name
        self.token = token
        self.address = rf"\\.\pipe\{pipe_name}"
        self.daemon_context = _normalize_context(daemon_context)
        self.owner_pid = int(owner_pid) if owner_pid else 0
        self.running = True
        self.state: Dict[str, Any] = {
            "pipe_name": pipe_name,
            "session_id": "",
            "capture_file_id": "",
            "active_event_id": 0,
            "daemon_context": self.daemon_context,
            "owner_pid": self.owner_pid,
        }
        self._listener = None
        self._stop_event = threading.Event()

    def _auth(self, request: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
        if str(request.get("token", "")) != self.token:
            return False, {"ok": False, "error": {"code": "unauthorized", "message": "invalid daemon token"}}
        return True, {}

    def _status_payload(self) -> Dict[str, Any]:
        return {"ok": True, "result": {"running": self.running, "state": dict(self.state)}}

    def _stop(self) -> None:
        self.running = False
        self._stop_event.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass

    def _watch_owner(self) -> None:
        if not self.owner_pid:
            return
        while self.running:
            if self._stop_event.wait(1.0):
                break
            if not _is_process_running(self.owner_pid):
                logger.info("daemon owner pid %s no longer running, shutting down", self.owner_pid)
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
            return {"ok": True, "result": {"pong": True}}
        if method == "status":
            return self._status_payload()
        if method == "shutdown":
            self._stop()
            return {"ok": True, "result": {"stopping": True}}
        if method == "set_state":
            self.state.update(dict(params))
            return {"ok": True, "result": {"state": dict(self.state)}}
        if method == "get_state":
            return {"ok": True, "result": {"state": dict(self.state)}}
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
            result = asyncio.run(dispatch_operation(operation, args, transport=transport, remote=remote))
            if isinstance(result, dict):
                meta = result.get("meta", {})
                logger.info(
                    "daemon.exec.done op=%s trace_id=%s ok=%s duration_ms=%s",
                    operation,
                    (meta.get("trace_id") if isinstance(meta, dict) else ""),
                    bool(result.get("ok")),
                    (meta.get("duration_ms") if isinstance(meta, dict) else None),
                )
            return {"ok": True, "result": result}
        return {"ok": False, "error": {"code": "unknown_method", "message": f"unknown method: {method}"}}

    def serve_forever(self) -> int:
        self._listener = Listener(address=self.address, family="AF_PIPE")
        logger.info("daemon listening on %s", self.address)
        try:
            threading.Thread(target=self._watch_owner, name="rdx-daemon-owner-monitor", daemon=True).start()
            while self.running:
                try:
                    conn = self._listener.accept()
                except Exception:
                    break
                try:
                    request = conn.recv()
                    conn.send(self.handle_request(request))
                except Exception as exc:  # noqa: BLE001
                    conn.send({"ok": False, "error": {"code": "daemon_error", "message": str(exc)}})
                finally:
                    conn.close()
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
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    bootstrap = bootstrap_renderdoc_runtime(probe_import=False)
    logger.info("runtime bootstrap dll_dir=%s pymodules=%s", bootstrap.binaries_dir, bootstrap.pymodules_dir)
    for item in bootstrap.dll_dir_errors:
        logger.warning("runtime bootstrap warning: %s", item)

    daemon = DaemonRuntime(
        pipe_name=str(args.pipe_name),
        token=str(args.token),
        daemon_context=str(args.daemon_context),
        owner_pid=int(args.owner_pid),
    )

    def _stop(*_a: Any) -> None:
        daemon._stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    asyncio.run(runtime_startup())
    try:
        raise SystemExit(daemon.serve_forever())
    finally:
        asyncio.run(runtime_shutdown())


if __name__ == "__main__":
    main()
