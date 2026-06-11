"""Upstream adapter factory."""
from __future__ import annotations

from ..config import UpstreamServerConfig
from .auth_headers import AuthHeaderProvider
from .base import UpstreamAdapter
from .custom import build_custom_adapter
from .sse_legacy import LegacySseAdapter
from .stdio import StdioAdapter
from .streamable_http import StreamableHttpAdapter


def build_adapter(
    cfg: UpstreamServerConfig,
    *,
    auth_header_provider: AuthHeaderProvider | None = None,
) -> UpstreamAdapter:
    if cfg.transport == "stdio":
        if not cfg.command:
            raise ValueError(f"{cfg.id}: stdio transport requires 'command'")
        return StdioAdapter(
            server_id=cfg.id,
            command=cfg.command,
            env=cfg.env,
            cwd=cfg.cwd,
            request_timeout_s=cfg.request_timeout_s,
        )
    if cfg.transport == "streamable_http":
        if not cfg.url:
            raise ValueError(f"{cfg.id}: streamable_http transport requires 'url'")
        return StreamableHttpAdapter(
            server_id=cfg.id,
            url=cfg.url,
            headers=cfg.headers,
            request_timeout_s=cfg.request_timeout_s,
            auth_header_provider=auth_header_provider,
        )
    if cfg.transport == "sse_legacy":
        if not cfg.sse_url:
            raise ValueError(f"{cfg.id}: sse_legacy transport requires 'sse_url'")
        return LegacySseAdapter(
            server_id=cfg.id,
            sse_url=cfg.sse_url,
            post_url=cfg.post_url,
            headers=cfg.headers,
            request_timeout_s=cfg.request_timeout_s,
            auth_header_provider=auth_header_provider,
        )
    if cfg.transport == "custom":
        if not cfg.custom_kind:
            raise ValueError(f"{cfg.id}: custom transport requires 'custom_kind'")
        return build_custom_adapter(cfg.custom_kind, cfg.id, cfg.custom_params)
    raise ValueError(f"{cfg.id}: unknown transport {cfg.transport}")
