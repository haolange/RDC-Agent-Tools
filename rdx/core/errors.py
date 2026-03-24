"""Core error taxonomy with stable categories for exit-code mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class CoreError(Exception):
    code: str
    message: str
    category: str
    details: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class ValidationError(CoreError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            code="validation_error",
            message=message,
            category="validation",
            details=dict(details or {}),
        )


class NotFoundError(CoreError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            code="not_found",
            message=message,
            category="not_found",
            details=dict(details or {}),
        )


class AssertionFailedError(CoreError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            code="assertion_failed",
            message=message,
            category="assertion_failed",
            details=dict(details or {}),
        )


class RuntimeToolError(CoreError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            code="runtime_error",
            message=message,
            category="runtime",
            details=dict(details or {}),
        )


class PermissionToolError(CoreError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            code="permission_error",
            message=message,
            category="permission",
            details=dict(details or {}),
        )


class IOToolError(CoreError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            code="io_error",
            message=message,
            category="io",
            details=dict(details or {}),
        )


class InternalToolError(CoreError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            code="internal_error",
            message=message,
            category="internal",
            details=dict(details or {}),
        )


def map_exception(exc: Exception) -> CoreError:
    if isinstance(exc, CoreError):
        return exc
    try:
        from rdx.core.session_manager import SessionError
    except Exception:
        SessionError = None  # type: ignore[assignment]
    if SessionError is not None and isinstance(exc, SessionError):
        detail = getattr(exc, "detail", None)
        code = str(getattr(detail, "code", "runtime_error") or "runtime_error")
        message = str(getattr(detail, "message", str(exc)) or str(exc))
        details = getattr(detail, "details", None)
        if not isinstance(details, dict):
            details = {}
        category = "not_found" if code.endswith("_not_found") or code == "session_not_found" else "runtime"
        return CoreError(
            code=code,
            message=message,
            category=category,
            details=dict(details),
        )
    if isinstance(exc, FileNotFoundError):
        return NotFoundError(str(exc))
    if isinstance(exc, PermissionError):
        return PermissionToolError(str(exc))
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return ValidationError(str(exc))
    if isinstance(exc, OSError):
        return IOToolError(str(exc))
    return InternalToolError(str(exc))

