from __future__ import annotations

from typing import Any, Dict

from rdx import server_runtime


async def handle(action: str, args: Dict[str, Any], env: Dict[str, Any]) -> Any:
    return await server_runtime._dispatch_remote(action, args)

