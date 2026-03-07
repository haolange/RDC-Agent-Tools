"""Timeout policy helpers shared by CLI and daemon-backed MCP."""

from __future__ import annotations

import math
from typing import Any

REMOTE_CONNECT_DEFAULT_TIMEOUT_MS = 200000
REMOTE_CONNECT_DAEMON_BUFFER_S = 15.0
REMOTE_OPEN_REPLAY_TIMEOUT_S = 200.0
LOCAL_OPEN_REPLAY_TIMEOUT_S = 60.0
DEFAULT_DAEMON_REQUEST_TIMEOUT_S = 10.0


def _coerce_timeout_ms(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def remote_connect_timeout_ms(args: dict[str, Any] | None) -> int:
    payload = args if isinstance(args, dict) else {}
    return _coerce_timeout_ms(payload.get("timeout_ms"), REMOTE_CONNECT_DEFAULT_TIMEOUT_MS)


def daemon_exec_timeout_s(operation: str, args: dict[str, Any] | None) -> float:
    payload = args if isinstance(args, dict) else {}

    if operation == "rd.remote.connect":
        timeout_ms = remote_connect_timeout_ms(payload)
        return float(math.ceil(timeout_ms / 1000.0) + REMOTE_CONNECT_DAEMON_BUFFER_S)

    if operation == "rd.capture.open_replay":
        options = payload.get("options")
        if isinstance(options, dict) and str(options.get("remote_id") or "").strip():
            return REMOTE_OPEN_REPLAY_TIMEOUT_S
        return LOCAL_OPEN_REPLAY_TIMEOUT_S

    return DEFAULT_DAEMON_REQUEST_TIMEOUT_S
