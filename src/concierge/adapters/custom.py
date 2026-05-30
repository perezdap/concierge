"""
Custom transport adapter — placeholder + factory hook.

Operators can register their own adapter classes by:

    from concierge.adapters.custom import register_custom_adapter
    register_custom_adapter("my-transport", MyAdapterClass)

and then setting `transport: custom` + `custom_kind: my-transport` in config.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import UpstreamAdapter

_FACTORIES: dict[str, Callable[..., UpstreamAdapter]] = {}


def register_custom_adapter(
    kind: str, factory: Callable[..., UpstreamAdapter] | type[UpstreamAdapter]
) -> None:
    _FACTORIES[kind] = factory


def build_custom_adapter(kind: str, server_id: str, params: dict[str, Any]) -> UpstreamAdapter:
    if kind not in _FACTORIES:
        raise ValueError(f"no custom adapter registered for kind={kind!r}")
    return _FACTORIES[kind](server_id=server_id, **params)
