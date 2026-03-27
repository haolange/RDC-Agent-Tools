from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from rdx import server
from rdx.context_snapshot import clear_context_snapshot
from rdx.runtime_state import clear_context_state


class _FakeCaptureFile:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def OpenFile(self, path: str, *_args) -> str:
        self.calls.append(f"OpenFile:{path}")
        return "ok"

    def DriverName(self) -> str:
        self.calls.append("DriverName")
        return "D3D12"

    def OpenCapture(self, *_args):
        raise AssertionError("rd.capture.open_file must not call OpenCapture")

    def CloseFile(self) -> None:
        self.calls.append("CloseFile")


def test_dispatch_capture_open_file_does_not_open_replay(monkeypatch, tmp_path) -> None:
    original_captures = dict(server._runtime.captures)
    fake_capture = _FakeCaptureFile()
    sample = tmp_path / "sample.rdc"
    sample.write_bytes(b"rdc")

    async def _inline_offload(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(server.server_runtime, "_offload", _inline_offload)
    monkeypatch.setattr(server.server_runtime, "_check_status", lambda status, operation, **kwargs: None)
    monkeypatch.setattr(server.server_runtime, "_get_rd", lambda: SimpleNamespace(OpenCaptureFile=lambda: fake_capture))

    clear_context_snapshot()
    clear_context_state()
    server._runtime.context_snapshots.clear()
    server._runtime.context_states.clear()
    server._runtime.hydrated_contexts.clear()
    server._runtime.captures.clear()
    try:
        payload = json.loads(asyncio.run(server._dispatch_capture("open_file", {"file_path": str(sample)})))
        assert payload["success"] is True
        assert payload["driver"] == "D3D12"
        assert fake_capture.calls == [f"OpenFile:{sample}", "DriverName", "CloseFile"]
    finally:
        clear_context_snapshot()
        clear_context_state()
        server._runtime.context_snapshots.clear()
        server._runtime.context_states.clear()
        server._runtime.hydrated_contexts.clear()
        server._runtime.captures.clear()
        server._runtime.captures.update(original_captures)
