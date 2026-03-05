"""Registry for unified core operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional


OperationHandler = Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[Any]]


@dataclass
class _Entry:
    name: str
    handler: OperationHandler


class OperationRegistry:
    def __init__(self) -> None:
        self._entries: Dict[str, _Entry] = {}
        self._default_handler: Optional[OperationHandler] = None

    def register(self, name: str, handler: OperationHandler) -> None:
        self._entries[str(name)] = _Entry(name=str(name), handler=handler)

    def register_many(self, names: list[str], handler: OperationHandler) -> None:
        for name in names:
            self.register(name, handler)

    def set_default(self, handler: OperationHandler) -> None:
        self._default_handler = handler

    def has(self, name: str) -> bool:
        return str(name) in self._entries or self._default_handler is not None

    def resolve(self, name: str) -> Optional[OperationHandler]:
        entry = self._entries.get(str(name))
        if entry is not None:
            return entry.handler
        return self._default_handler

    def list_names(self) -> list[str]:
        return sorted(self._entries.keys())

