"""GatewayService method coverage beyond discovery tests."""
from __future__ import annotations

import pytest

from concierge.errors import InvalidParams, MethodNotFound
from tests.test_discovery import _svc


@pytest.mark.asyncio
async def test_dispatch_unknown_method():
    svc = await _svc()
    s = await svc.sessions.create()
    with pytest.raises(MethodNotFound):
        await svc.dispatch(s, "bogus/method", {})


@pytest.mark.asyncio
async def test_tools_call_invalid_params():
    svc = await _svc()
    s = await svc.sessions.create()
    with pytest.raises(InvalidParams):
        await svc.tools_call(s, {"name": 123})


@pytest.mark.asyncio
async def test_resources_list_empty_when_none_published():
    svc = await _svc()
    s = await svc.sessions.create()
    res = await svc.resources_list(s)
    assert res["resources"] == []


@pytest.mark.asyncio
async def test_ping_via_dispatch():
    svc = await _svc()
    s = await svc.sessions.create()
    assert await svc.dispatch(s, "ping", {}) == {}
