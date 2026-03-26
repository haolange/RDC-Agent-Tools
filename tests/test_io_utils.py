from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

from rdx import cli as rdx_cli
from rdx.io_utils import AtomicWriteError, atomic_write_json, safe_stream_write


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_MCP_PATH = REPO_ROOT / "mcp" / "run_mcp.py"


def _load_run_mcp():
    spec = importlib.util.spec_from_file_location("rdx_run_mcp_test_module", RUN_MCP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _AsciiStream(io.StringIO):
    encoding = "ascii"

    def write(self, s: str) -> int:
        s.encode(self.encoding)
        return super().write(s)


def test_safe_stream_write_backslash_replaces_invalid_unicode() -> None:
    stream = _AsciiStream()

    safe_stream_write("bad-\udcff", stream)

    assert stream.getvalue() == "bad-\\udcff"


def test_cli_print_json_handles_invalid_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _AsciiStream()
    monkeypatch.setattr(rdx_cli.sys, "stdout", stream)

    rdx_cli._print_json({"value": "bad-\udcff"})

    assert "\\udcff" in stream.getvalue()


def test_mcp_emit_payload_handles_invalid_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    run_mcp = _load_run_mcp()
    stream = _AsciiStream()
    monkeypatch.setattr(run_mcp.sys, "stdout", stream)

    run_mcp._emit_payload({"value": "bad-\udcff"})

    assert "\\udcff" in stream.getvalue()


def test_atomic_write_json_cleans_tmp_on_replace_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"old": true}', encoding="utf-8")

    def _always_fail(src: str | Path, dst: str | Path) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr("rdx.io_utils.os.replace", _always_fail)

    with pytest.raises(AtomicWriteError) as exc:
        atomic_write_json(target, {"new": True})

    assert target.read_text(encoding="utf-8") == '{"old": true}'
    assert not list(tmp_path.glob("*.tmp"))
    assert "final_path" in exc.value.details
