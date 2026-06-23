from __future__ import annotations

import pytest

from rdx import cli as rdx_cli


def test_load_call_args_accepts_args_json_object() -> None:
    payload = rdx_cli._load_call_args(args_json='{"session_id":"sess-001","event_id":7}')
    assert payload == {"session_id": "sess-001", "event_id": 7}


def test_load_call_args_accepts_args_file_object(tmp_path) -> None:
    args_file = tmp_path / "args.json"
    args_file.write_text('{"session_id":"sess-002","projection":{"kind":"tabular"}}', encoding="utf-8")

    payload = rdx_cli._load_call_args(args_file=str(args_file))

    assert payload == {"session_id": "sess-002", "projection": {"kind": "tabular"}}


def test_load_call_args_accepts_utf8_bom_args_file(tmp_path) -> None:
    args_file = tmp_path / "args-bom.json"
    args_file.write_text('{"enable_remote":true}', encoding="utf-8-sig")

    payload = rdx_cli._load_call_args(args_file=str(args_file))

    assert payload == {"enable_remote": True}


def test_load_call_args_rejects_args_json_and_args_file_together(tmp_path) -> None:
    args_file = tmp_path / "args.json"
    args_file.write_text('{"session_id":"sess-003"}', encoding="utf-8")

    with pytest.raises(ValueError, match="mutually exclusive"):
        rdx_cli._load_call_args(args_json='{"session_id":"sess-003"}', args_file=str(args_file))


def test_load_call_args_rejects_invalid_args_json() -> None:
    with pytest.raises(ValueError, match=r"--args-json contains invalid JSON"):
        rdx_cli._load_call_args(args_json='{"session_id": }')


def test_load_call_args_recovers_raw_windows_args_json(monkeypatch) -> None:
    monkeypatch.setattr(
        rdx_cli,
        "_recover_args_json_from_command_line",
        lambda: '{"session_id":"sess-raw","event_id":7,"projection":{"kind":"tabular"}}',
    )

    payload = rdx_cli._load_call_args(args_json="{session_id:sess-raw,event_id:7,projection:{kind:tabular}}")

    assert payload == {"session_id": "sess-raw", "event_id": 7, "projection": {"kind": "tabular"}}


def test_load_call_args_rejects_non_object_args_json() -> None:
    with pytest.raises(ValueError, match=r"--args-json must be a JSON object"):
        rdx_cli._load_call_args(args_json='["sess-004"]')


def test_extract_raw_args_json_from_command_line_reads_balanced_payload() -> None:
    raw = (
        '"python.exe" cli/run_cli.py call rd.core.init '
        '--args-json {"session_id":"sess-raw","projection":{"kind":"tabular"}} --format json'
    )

    extracted = rdx_cli._extract_raw_args_json_from_command_line(raw)

    assert extracted == '{"session_id":"sess-raw","projection":{"kind":"tabular"}}'


def test_load_call_args_rejects_missing_args_file(tmp_path) -> None:
    missing = tmp_path / "missing.json"

    with pytest.raises(ValueError, match=r"--args-file could not be read"):
        rdx_cli._load_call_args(args_file=str(missing))


def test_load_call_args_rejects_invalid_args_file_json(tmp_path) -> None:
    args_file = tmp_path / "args.json"
    args_file.write_text('{"session_id": }', encoding="utf-8")

    with pytest.raises(ValueError, match=r"--args-file contains invalid JSON"):
        rdx_cli._load_call_args(args_file=str(args_file))


def test_load_call_args_rejects_non_object_args_file(tmp_path) -> None:
    args_file = tmp_path / "args.json"
    args_file.write_text('["sess-005"]', encoding="utf-8")

    with pytest.raises(ValueError, match=r"--args-file must be a JSON object"):
        rdx_cli._load_call_args(args_file=str(args_file))


def test_load_call_args_invalid_complex_json_points_to_args_file() -> None:
    with pytest.raises(ValueError) as excinfo:
        rdx_cli._load_call_args(args_json='{"source_text":"float4 main() : SV_Target { return 0; }"')

    message = str(excinfo.value)
    assert "--args-json contains invalid JSON" in message
    assert "Use --args-file args.json for multiline shader source" in message
    assert "rd.shader.edit_and_replace --args-file args.json" in message