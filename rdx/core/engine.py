"""Unified execution engine shared by MCP, CLI, and daemon."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .artifact_publisher import ArtifactPublisher
from .contracts import canonical_error, canonical_success, collect_artifact_candidates
from .errors import CoreError, InternalToolError, NotFoundError, map_exception
from .operation_registry import OperationRegistry
from rdx.progress import ProgressReporter

logger = logging.getLogger(__name__)


@dataclass
class ExecutionContext:
    transport: str = "core"
    remote: bool = False
    trace_id: str = field(default_factory=lambda: f"trc_{uuid.uuid4().hex[:12]}")
    metadata: Dict[str, Any] = field(default_factory=dict)
    progress_reporter: Optional[ProgressReporter] = None


class CoreEngine:
    def __init__(
        self,
        *,
        registry: OperationRegistry,
        artifact_publisher: Optional[ArtifactPublisher] = None,
    ) -> None:
        self.registry = registry
        self.artifact_publisher = artifact_publisher or ArtifactPublisher()

    async def execute(
        self,
        operation: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        context: Optional[ExecutionContext] = None,
    ) -> Dict[str, Any]:
        start = time.perf_counter()
        ctx = context or ExecutionContext()
        payload_args = dict(args or {})
        try:
            handler = self.registry.resolve(operation)
            if handler is None:
                raise NotFoundError(f"Unsupported operation: {operation}")
            raw = await handler(payload_args, {"operation": operation, "context": ctx})
            normalized = await self._normalize_output(operation, raw, ctx)
            duration_ms = int((time.perf_counter() - start) * 1000)
            normalized.setdefault("meta", {})
            normalized["meta"].setdefault("trace_id", ctx.trace_id)
            normalized["meta"].setdefault("transport", ctx.transport)
            normalized["meta"].setdefault("duration_ms", duration_ms)
            return normalized
        except Exception as exc:  # noqa: BLE001
            core_exc = map_exception(exc)
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.debug("Core execution failed for %s: %s", operation, core_exc)
            return canonical_error(
                result_kind=str(operation),
                code=core_exc.code,
                category=core_exc.category,
                message=core_exc.message,
                details=core_exc.details,
                trace_id=ctx.trace_id,
                transport=ctx.transport,
                duration_ms=duration_ms,
            )

    async def _normalize_output(
        self,
        operation: str,
        raw: Any,
        ctx: ExecutionContext,
    ) -> Dict[str, Any]:
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    raw = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise InternalToolError(
                        f"Operation returned non-JSON string for {operation}",
                        {"preview": text[:160]},
                    ) from exc
            else:
                raw = {"raw": raw}

        if isinstance(raw, dict) and all(
            key in raw for key in ("schema_version", "tool_version", "result_kind", "ok", "data", "artifacts", "error")
        ):
            return raw

        if not isinstance(raw, dict):
            raw = {"value": raw}

        if "success" in raw or "error_message" in raw:
            success = bool(raw.get("success", True))
            if success:
                data = {
                    k: v
                    for k, v in raw.items()
                    if k not in {"success", "error_message", "ok", "error", "schema_version", "tool_version", "result_kind", "data", "artifacts", "meta"}
                }
                artifacts = await self.artifact_publisher.publish_candidates(
                    collect_artifact_candidates(raw),
                    remote=ctx.remote,
                )
                return canonical_success(
                    result_kind=str(operation),
                    data=data,
                    artifacts=artifacts,
                    trace_id=ctx.trace_id,
                    transport=ctx.transport,
                )
            error_message = str(raw.get("error_message") or "Operation failed")
            details = {
                k: v
                for k, v in raw.items()
                if k
                not in {
                    "success",
                    "error_message",
                    "ok",
                    "error",
                    "schema_version",
                    "tool_version",
                    "result_kind",
                    "data",
                    "artifacts",
                    "meta",
                    "code",
                    "error_code",
                    "category",
                    "details",
                }
            }
            explicit_details = raw.get("details")
            if isinstance(explicit_details, dict):
                details = {**explicit_details, **details}
            return canonical_error(
                result_kind=str(operation),
                code=str(raw.get("code") or raw.get("error_code") or "runtime_error"),
                category=str(raw.get("category") or "runtime"),
                message=error_message,
                details=details,
                trace_id=ctx.trace_id,
                transport=ctx.transport,
            )

        if "ok" in raw and isinstance(raw.get("data"), dict):
            success = bool(raw.get("ok"))
            if success:
                artifacts = list(raw.get("artifacts") or [])
                if not artifacts:
                    artifacts = await self.artifact_publisher.publish_candidates(
                        collect_artifact_candidates(raw.get("data") or {}),
                        remote=ctx.remote,
                    )
                return canonical_success(
                    result_kind=str(raw.get("result_kind") or operation),
                    data=dict(raw.get("data") or {}),
                    artifacts=artifacts,
                    meta=dict(raw.get("meta") or {}),
                    trace_id=ctx.trace_id,
                    transport=ctx.transport,
                )
            err = raw.get("error") if isinstance(raw.get("error"), dict) else {}
            return canonical_error(
                result_kind=str(raw.get("result_kind") or operation),
                code=str(err.get("code") or "runtime_error"),
                category=str(err.get("category") or "runtime"),
                message=str(err.get("message") or "Operation failed"),
                details=dict(err.get("details") or {}),
                artifacts=list(raw.get("artifacts") or []),
                meta=dict(raw.get("meta") or {}),
                trace_id=ctx.trace_id,
                transport=ctx.transport,
            )

        artifacts = await self.artifact_publisher.publish_candidates(
            collect_artifact_candidates(raw),
            remote=ctx.remote,
        )
        return canonical_success(
            result_kind=str(operation),
            data=raw,
            artifacts=artifacts,
            trace_id=ctx.trace_id,
            transport=ctx.transport,
        )
