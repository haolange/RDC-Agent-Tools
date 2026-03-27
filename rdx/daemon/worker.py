"""Worker process lifecycle for daemon-owned RenderDoc runtime."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from rdx.io_utils import safe_json_text
from rdx.runtime_materializer import MaterializedRuntime, materialize_runtime
from rdx.runtime_worker_state import clear_worker_state, save_worker_state


READY_TIMEOUT_S = 10.0
REQUEST_TIMEOUT_S = 30.0


@dataclass
class WorkerSnapshot:
    context_id: str
    pid: int
    running: bool
    runtime_id: str
    cache_root: str
    source_manifest: str


class RuntimeWorkerProcess:
    def __init__(self, *, context_id: str) -> None:
        self.context_id = str(context_id)
        self._proc: Optional[subprocess.Popen[str]] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._io_lock = threading.Lock()
        self._request_seq = 0
        self._runtime: Optional[MaterializedRuntime] = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def snapshot(self) -> Dict[str, Any]:
        runtime = self._runtime
        return {
            "running": self.is_running(),
            "pid": int(self._proc.pid) if self._proc and self.is_running() else 0,
            "runtime_id": runtime.runtime_id if runtime else "",
            "cache_root": str(runtime.cache_root) if runtime else "",
            "source_manifest": str(runtime.source_manifest) if runtime else "",
        }

    def _save_state(self) -> None:
        payload = {
            "context_id": self.context_id,
            **self.snapshot(),
        }
        if payload["running"]:
            save_worker_state(payload, context=self.context_id)
        else:
            clear_worker_state(self.context_id)

    def _pump_stdout(self, stdout: Any) -> None:
        for line in iter(stdout.readline, ""):
            text = str(line).strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                payload = {"kind": "invalid", "raw": text}
            self._queue.put(payload)
        self._queue.put({"kind": "eof"})

    def _spawn(self) -> None:
        runtime = materialize_runtime()
        env = dict(os.environ)
        env["RDX_CONTEXT_ID"] = self.context_id
        env["RDX_RUNTIME_DLL_DIR"] = str(runtime.binaries_dir)
        env["RDX_RENDERDOC_PATH"] = str(runtime.pymodules_dir)
        env["RDX_WORKER_RUNTIME_ID"] = runtime.runtime_id
        env["RDX_WORKER_CACHE_ROOT"] = str(runtime.cache_root)
        env["RDX_WORKER_SOURCE_MANIFEST"] = str(runtime.source_manifest)

        tools_root = str(env.get("RDX_TOOLS_ROOT", "")).strip()
        if tools_root:
            py_path = env.get("PYTHONPATH", "")
            root_path = str(Path(tools_root).resolve())
            env["PYTHONPATH"] = root_path + os.pathsep + py_path if py_path else root_path

        popen_kwargs: Dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "bufsize": 1,
            "env": env,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(
            [sys.executable, "-m", "rdx.runtime_worker", "--context-id", self.context_id],
            **popen_kwargs,
        )
        assert proc.stdout is not None
        self._proc = proc
        self._runtime = runtime
        self._queue = queue.Queue()
        self._reader_thread = threading.Thread(
            target=self._pump_stdout,
            args=(proc.stdout,),
            name=f"rdx-worker-reader-{self.context_id}",
            daemon=True,
        )
        self._reader_thread.start()
        self._await_ready()
        self._save_state()

    def _await_ready(self) -> None:
        deadline = time.time() + READY_TIMEOUT_S
        while time.time() < deadline:
            timeout = max(0.1, min(0.5, deadline - time.time()))
            try:
                payload = self._queue.get(timeout=timeout)
            except queue.Empty:
                continue
            kind = str(payload.get("kind") or "")
            if kind == "ready":
                return
            if kind == "eof":
                break
            if kind == "startup_error":
                raise RuntimeError(str(payload.get("message") or "worker startup failed"))
        raise RuntimeError("worker failed to report ready state")

    def ensure_started(self) -> None:
        if self.is_running():
            return
        self.stop()
        self._spawn()

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, *, timeout: float = REQUEST_TIMEOUT_S) -> Dict[str, Any]:
        with self._io_lock:
            self.ensure_started()
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise RuntimeError("worker process is unavailable")
            self._request_seq += 1
            req_id = f"wrk_{self._request_seq}"
            proc.stdin.write(
                safe_json_text({"id": req_id, "method": str(method), "params": dict(params or {})}) + "\n"
            )
            proc.stdin.flush()

            deadline = time.time() + max(1.0, float(timeout))
            while time.time() < deadline:
                remaining = max(0.1, min(0.5, deadline - time.time()))
                try:
                    payload = self._queue.get(timeout=remaining)
                except queue.Empty:
                    continue
                if str(payload.get("kind") or "") == "eof":
                    raise RuntimeError("worker exited before responding")
                if str(payload.get("id") or "") != req_id:
                    continue
                self._save_state()
                return payload
            raise TimeoutError(f"worker request timed out: {method}")

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            clear_worker_state(self.context_id)
            self._runtime = None
            return
        try:
            if proc.poll() is None:
                try:
                    self.request("shutdown", {}, timeout=5.0)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5.0)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=3.0)
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            self._proc = None
            self._runtime = None
            clear_worker_state(self.context_id)
