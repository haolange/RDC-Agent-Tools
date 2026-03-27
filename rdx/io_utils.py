from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, TextIO


class AtomicWriteError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


def _sanitize_json_value(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {str(key): _sanitize_json_value(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_sanitize_json_value(item) for item in payload]
    if isinstance(payload, tuple):
        return [_sanitize_json_value(item) for item in payload]
    if isinstance(payload, str):
        try:
            payload.encode("utf-8")
            return payload
        except UnicodeEncodeError:
            return payload.encode("utf-8", errors="backslashreplace").decode("utf-8")
    return payload


def safe_json_text(payload: Any, *, indent: int | None = None, sort_keys: bool = False) -> str:
    return json.dumps(
        _sanitize_json_value(payload),
        ensure_ascii=False,
        indent=indent,
        sort_keys=sort_keys,
        default=str,
    )


def safe_stream_write(text: str, stream: TextIO) -> None:
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = str(getattr(stream, "encoding", "") or "utf-8")
        sanitized = text.encode(encoding, errors="backslashreplace").decode(encoding, errors="strict")
        stream.write(sanitized)
    stream.flush()


def _cleanup_path(path: Path) -> None:
    try:
        if not path.exists():
            return
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except Exception:
        return


def atomic_swap_path(
    temp_path: Path | str,
    final_path: Path | str,
    *,
    retries: int = 5,
    retry_delay_s: float = 0.05,
) -> None:
    temp = Path(temp_path)
    final = Path(final_path)
    backup = final.with_name(f"{final.name}.{secrets.token_hex(8)}.bak")
    last_error: BaseException | None = None
    promoted = False
    backed_up = False
    try:
        for attempt in range(retries + 1):
            try:
                if temp.is_dir() and final.exists():
                    os.replace(final, backup)
                    backed_up = True
                os.replace(temp, final)
                promoted = True
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if backed_up and not promoted:
                    try:
                        if not final.exists():
                            os.replace(backup, final)
                        backed_up = False
                    except Exception:
                        pass
                if attempt >= retries:
                    break
                time.sleep(retry_delay_s)
        if not promoted:
            details = {
                "temp_path": str(temp),
                "final_path": str(final),
                "retries": int(retries),
            }
            if last_error is not None:
                details["reason"] = str(last_error)
            raise AtomicWriteError("atomic swap failed", details=details)
    finally:
        if promoted and backup.exists():
            _cleanup_path(backup)
        if temp.exists():
            _cleanup_path(temp)


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Path:
    final = Path(path)
    final.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{final.name}.", suffix=".tmp", dir=str(final.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
        atomic_swap_path(tmp_path, final)
        return final
    except Exception:
        _cleanup_path(tmp_path)
        raise


def atomic_write_json(
    path: Path | str,
    payload: Any,
    *,
    indent: int = 2,
    sort_keys: bool = False,
) -> Path:
    text = safe_json_text(payload, indent=indent, sort_keys=sort_keys)
    return atomic_write_text(path, text)


def atomic_append_jsonl(path: Path | str, entry: dict[str, Any]) -> Path:
    final = Path(path)
    existing = ""
    if final.exists():
        existing = final.read_text(encoding="utf-8")
    line = safe_json_text(entry) + "\n"
    if existing:
        text = existing if existing.endswith("\n") else existing + "\n"
        text += line
    else:
        text = line
    return atomic_write_text(final, text)
