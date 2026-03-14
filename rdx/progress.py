"""Structured progress reporting shared by daemon, MCP, and runtime handlers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


def _now_ms() -> int:
    return int(time.time() * 1000)


class ProgressSink(Protocol):
    def publish(self, event: "ProgressEvent") -> None:
        ...


@dataclass
class ProgressEvent:
    trace_id: str
    operation: str
    stage: str
    message: str
    progress_pct: Optional[float] = None
    details: Dict[str, Any] = field(default_factory=dict)
    ts_ms: int = field(default_factory=_now_ms)


@dataclass
class ProgressState:
    trace_id: str = ""
    operation: str = ""
    stage: str = ""
    message: str = ""
    progress_pct: Optional[float] = None
    details: Dict[str, Any] = field(default_factory=dict)
    started_at_ms: int = 0
    updated_at_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "operation": self.operation,
            "stage": self.stage,
            "message": self.message,
            "progress_pct": self.progress_pct,
            "details": dict(self.details),
            "started_at_ms": int(self.started_at_ms or 0),
            "updated_at_ms": int(self.updated_at_ms or 0),
        }


class ProgressReporter:
    def __init__(
        self,
        *,
        trace_id: str,
        operation: str,
        sink: Optional[ProgressSink] = None,
    ) -> None:
        self.trace_id = str(trace_id)
        self.operation = str(operation)
        self.sink = sink

    def emit(
        self,
        stage: str,
        message: str,
        *,
        progress_pct: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> ProgressEvent:
        event = ProgressEvent(
            trace_id=self.trace_id,
            operation=self.operation,
            stage=str(stage),
            message=str(message),
            progress_pct=progress_pct,
            details=dict(details or {}),
        )
        if self.sink is not None:
            self.sink.publish(event)
        return event

