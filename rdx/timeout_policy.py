"""Timeout policy helpers shared by CLI, MCP, and contract harnesses."""

from __future__ import annotations

import math
from typing import Any

REMOTE_CONNECT_DEFAULT_TIMEOUT_MS = 200000
REMOTE_CONNECT_DAEMON_BUFFER_S = 90.0
DAEMON_RESPONSE_BUFFER_S = 5.0
REMOTE_OPEN_REPLAY_TIMEOUT_S = 200.0
LOCAL_OPEN_REPLAY_TIMEOUT_S = 60.0
SESSION_CONTEXT_TIMEOUT_S = 10.0
PIXEL_HISTORY_TIMEOUT_S = 20.0
DEFAULT_DAEMON_REQUEST_TIMEOUT_S = 30.0
HEAVY_DAEMON_REQUEST_TIMEOUT_S = 60.0
VERY_HEAVY_DAEMON_REQUEST_TIMEOUT_S = 180.0
HARNESS_DEFAULT_TIMEOUT_S = 25.0

HEAVY_TIMEOUT_PREFIXES = (
    "rd.event.",
    "rd.pipeline.",
    "rd.resource.",
    "rd.texture.",
    "rd.buffer.",
    "rd.mesh.",
    "rd.shader.",
    "rd.debug.",
    "rd.perf.",
    "rd.export.",
    "rd.diag.",
    "rd.macro.",
    "rd.vfs.",
)

VERY_HEAVY_TIMEOUT_PREFIXES = (
    "rd.macro.",
    "rd.vfs.",
)


def _coerce_timeout_ms(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def remote_connect_timeout_ms(args: dict[str, Any] | None) -> int:
    payload = args if isinstance(args, dict) else {}
    return _coerce_timeout_ms(payload.get("timeout_ms"), REMOTE_CONNECT_DEFAULT_TIMEOUT_MS)


def operation_timeout_s(operation: str, args: dict[str, Any] | None) -> float:
    payload = args if isinstance(args, dict) else {}

    if operation == "rd.remote.connect":
        timeout_ms = remote_connect_timeout_ms(payload)
        return float(math.ceil(timeout_ms / 1000.0))

    if operation == "rd.capture.open_replay":
        options = payload.get("options")
        if isinstance(options, dict) and str(options.get("remote_id") or "").strip():
            return REMOTE_OPEN_REPLAY_TIMEOUT_S
        return LOCAL_OPEN_REPLAY_TIMEOUT_S

    if operation.startswith("rd.session."):
        return SESSION_CONTEXT_TIMEOUT_S

    if operation in {"rd.capture.close_replay", "rd.remote.disconnect"}:
        return 90.0

    if operation.startswith(VERY_HEAVY_TIMEOUT_PREFIXES):
        return VERY_HEAVY_DAEMON_REQUEST_TIMEOUT_S

    if operation.startswith(HEAVY_TIMEOUT_PREFIXES):
        return HEAVY_DAEMON_REQUEST_TIMEOUT_S

    return DEFAULT_DAEMON_REQUEST_TIMEOUT_S


def transport_timeout_s(operation: str, args: dict[str, Any] | None) -> float:
    base = operation_timeout_s(operation, args)
    if operation == "rd.remote.connect":
        return base + REMOTE_CONNECT_DAEMON_BUFFER_S
    return base + DAEMON_RESPONSE_BUFFER_S


def daemon_exec_timeout_s(operation: str, args: dict[str, Any] | None) -> float:
    return transport_timeout_s(operation, args)


def worker_exec_timeout_s(operation: str, args: dict[str, Any] | None) -> float:
    return operation_timeout_s(operation, args)


def harness_timeout_s(operation: str, args: dict[str, Any] | None) -> float:
    return max(HARNESS_DEFAULT_TIMEOUT_S, transport_timeout_s(operation, args))
