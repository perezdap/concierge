"""
UpstreamAdapter interface.

Each adapter normalizes a single upstream MCP server. The gateway never talks
to upstream transports directly; it only talks through this interface.

Design notes
------------
* Methods return normalized dicts (the raw MCP shapes). Adapters do not run
  sanitization — that happens in the catalog layer so the policy is centralized.
* `list_changed` is exposed as an async iterator. Adapters that cannot natively
  notify (e.g., poorly-behaved stdio servers) fall back to a polling iterator
  provided in `AdapterManager`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from ..core.types import AdapterHealth, TransportType


class UpstreamAdapter(ABC):
    server_id: str
    transport: TransportType

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def initialize(self) -> dict[str, Any]:
        """Run MCP `initialize` and return upstream server info / capabilities."""

    @abstractmethod
    async def list_tools(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def list_resources(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def list_prompts(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def read_resource(self, uri: str) -> dict[str, Any]: ...

    @abstractmethod
    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def list_changed_events(self) -> AsyncIterator[str]:
        """Yields one of 'tools' | 'resources' | 'prompts' when upstream signals
        a list change. Adapters that don't support this should not override —
        the AdapterManager falls back to periodic polling.
        """
        if False:  # pragma: no cover  — for typing
            yield ""
        return

    @abstractmethod
    def health(self) -> AdapterHealth: ...
