"""App factory helper coverage (_build_adapter, _build_auth)."""
from __future__ import annotations

import pytest

from concierge.config import AuthConfig, GatewayConfig, PolicyConfig, UpstreamServerConfig
from concierge.policy.approval import AllowListApprovalBroker, DenyByDefaultApprovalBroker
from concierge.server.app import _build_adapter, _build_approval, _build_auth
from concierge.server.auth import LocalhostAllowAuth, NoAuth, StaticBearerAuth


@pytest.mark.parametrize(
    "transport,missing_field,match",
    [
        ("stdio", {}, "command"),
        ("streamable_http", {}, "url"),
        ("sse_legacy", {}, "sse_url"),
        ("custom", {}, "custom_kind"),
    ],
)
def test_build_adapter_validates_required_fields(
    transport: str, missing_field: dict, match: str
) -> None:
    cfg = UpstreamServerConfig(id="srv", transport=transport, **missing_field)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=match):
        _build_adapter(cfg)


def test_build_adapter_unknown_transport():
    cfg = UpstreamServerConfig.model_construct(id="srv", transport="unknown")
    with pytest.raises(ValueError, match="unknown transport"):
        _build_adapter(cfg)


def test_build_adapter_stdio_success():
    from concierge.adapters.stdio import StdioAdapter

    cfg = UpstreamServerConfig(id="echo", transport="stdio", command=["python", "-c", "pass"])
    adapter = _build_adapter(cfg)
    assert isinstance(adapter, StdioAdapter)


def test_build_auth_variants():
    assert isinstance(_build_auth(GatewayConfig(auth=AuthConfig(type="none"))), NoAuth)
    assert isinstance(
        _build_auth(GatewayConfig(auth=AuthConfig(type="bearer", bearer_tokens=["t"]))),
        StaticBearerAuth,
    )
    assert isinstance(
        _build_auth(GatewayConfig(auth=AuthConfig(type="localhost"))),
        LocalhostAllowAuth,
    )


def test_build_approval_modes():
    assert isinstance(_build_approval(GatewayConfig()), DenyByDefaultApprovalBroker)
    cfg = GatewayConfig(
        policy=PolicyConfig(approval_mode="allow_list", approval_allow_list=["demo__alpha"]),
    )
    assert isinstance(_build_approval(cfg), AllowListApprovalBroker)
