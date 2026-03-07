"""Structured RenderDoc status helpers for runtime and session error reporting."""

from __future__ import annotations

from typing import Any, Dict, Optional

from rdx.core.errors import RuntimeToolError


def status_ok(status: Any, rd_module: Any | None = None) -> bool:
    try:
        ok = getattr(status, "OK", None)
        if callable(ok):
            return bool(ok())
    except Exception:
        pass
    if rd_module is not None:
        try:
            return status == rd_module.ResultCode.Succeeded
        except Exception:
            pass
    return False


def status_text(status: Any) -> str:
    try:
        message = getattr(status, "Message", None)
        if callable(message):
            text = str(message() or "").strip()
            if text:
                return text
    except Exception:
        pass
    return str(status)


def status_code_name(status: Any) -> str:
    try:
        name = getattr(status, "name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        pass
    text = str(status)
    if text.startswith("ResultCode."):
        return text.split(".", 1)[1]
    return text


def status_code_raw(status: Any) -> str:
    try:
        return str(int(status))
    except Exception:
        return str(status)


def build_renderdoc_error_details(
    status: Any,
    *,
    operation: str,
    source_layer: str,
    backend_type: str,
    capture_context: Optional[Dict[str, Any]] = None,
    classification: str,
    fix_hint: str,
) -> Dict[str, Any]:
    return {
        "source_layer": source_layer,
        "operation": str(operation),
        "backend_type": str(backend_type or "unknown"),
        "capture_context": dict(capture_context or {}),
        "renderdoc_status": {
            "result_code_raw": status_code_raw(status),
            "result_code_name": status_code_name(status),
            "status_text": status_text(status),
        },
        "classification": str(classification or "renderdoc_status"),
        "fix_hint": str(fix_hint or "Inspect the RenderDoc status and capture context before retrying."),
    }


def raise_renderdoc_error(
    status: Any,
    *,
    operation: str,
    source_layer: str,
    backend_type: str,
    capture_context: Optional[Dict[str, Any]] = None,
    classification: str = "renderdoc_status",
    fix_hint: str = "Inspect the RenderDoc status and capture context before retrying.",
) -> None:
    details = build_renderdoc_error_details(
        status,
        operation=operation,
        source_layer=source_layer,
        backend_type=backend_type,
        capture_context=capture_context,
        classification=classification,
        fix_hint=fix_hint,
    )
    raise RuntimeToolError(
        f"{operation} failed with status: {details['renderdoc_status']['status_text']}",
        details=details,
    )
