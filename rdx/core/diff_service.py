"""Unified diff wrappers built on top of CoreEngine operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .engine import CoreEngine, ExecutionContext


class DiffService:
    async def diff_pipeline(
        self,
        engine: CoreEngine,
        *,
        session_id: str,
        event_a: int,
        event_b: int,
        context: Optional[ExecutionContext] = None,
    ) -> Dict[str, Any]:
        return await engine.execute(
            "rd.event.diff_pipeline_state",
            {
                "session_id": str(session_id),
                "event_a": int(event_a),
                "event_b": int(event_b),
            },
            context=context,
        )

    async def diff_image(
        self,
        engine: CoreEngine,
        *,
        image_a_path: str,
        image_b_path: str,
        output_path: Optional[str] = None,
        context: Optional[ExecutionContext] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {
            "image_a_path": str(Path(image_a_path)),
            "image_b_path": str(Path(image_b_path)),
        }
        if output_path:
            args["output_path"] = str(Path(output_path))
        return await engine.execute(
            "rd.util.diff_images",
            args,
            context=context,
        )

