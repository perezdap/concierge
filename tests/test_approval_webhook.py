"""P1-3 approval webhook signing, verification, and retry/backoff."""
from __future__ import annotations

import json

import pytest

from concierge.policy.webhook import (
    SIGNATURE_HEADER,
    WebhookConfig,
    WebhookDispatcher,
    sign_payload,
    verify_signature,
)


def test_signature_roundtrip_verifies():
    body = b'{"event":"approval.granted"}'
    sig = sign_payload("s3cret", body)
    assert sig.startswith("sha256=")
    assert verify_signature("s3cret", body, sig)


def test_signature_rejects_wrong_secret_or_body():
    body = b'{"a":1}'
    sig = sign_payload("right", body)
    assert not verify_signature("wrong", body, sig)
    assert not verify_signature("right", b'{"a":2}', sig)
    assert not verify_signature("right", body, "sha256=deadbeef")
    assert not verify_signature("right", body, "")


class _Recorder:
    """Captures POSTs and replies with a scripted sequence of status codes."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        self.calls.append((url, content, headers))
        code = self.statuses.pop(0) if self.statuses else 200

        class _Resp:
            status_code = code

        return _Resp()


def _patch_client(monkeypatch, recorder):
    import concierge.policy.webhook as wh

    monkeypatch.setattr(wh.httpx, "AsyncClient", lambda *a, **k: recorder)


@pytest.mark.asyncio
async def test_dispatch_signs_and_delivers(monkeypatch):
    rec = _Recorder([200])
    _patch_client(monkeypatch, rec)
    cfg = WebhookConfig(default_urls=["http://hook"], default_secret="k")
    disp = WebhookDispatcher(cfg)
    await disp.dispatch(
        event="approval.granted",
        record_public={"approval_id": "ap_1", "tenant_id": "acme", "tool": "t"},
    )
    assert len(rec.calls) == 1
    url, body, headers = rec.calls[0]
    assert url == "http://hook"
    # The signature header verifies against the exact body bytes that were sent.
    assert verify_signature("k", body, headers[SIGNATURE_HEADER])
    payload = json.loads(body)
    assert payload["event"] == "approval.granted"
    assert payload["approval"]["approval_id"] == "ap_1"


@pytest.mark.asyncio
async def test_dispatch_retries_then_succeeds(monkeypatch):
    # Two failures then a success → 3 attempts, no audit failure.
    rec = _Recorder([500, 503, 200])
    _patch_client(monkeypatch, rec)
    cfg = WebhookConfig(
        default_urls=["http://hook"], default_secret="k",
        max_attempts=4, backoff_base_s=0.0, backoff_max_s=0.0,
    )
    disp = WebhookDispatcher(cfg)
    await disp.dispatch(event="approval.denied", record_public={"tenant_id": "acme"})
    assert len(rec.calls) == 3


@pytest.mark.asyncio
async def test_dispatch_gives_up_after_cap_and_audits(monkeypatch):
    rec = _Recorder([500, 500, 500])
    _patch_client(monkeypatch, rec)
    events = []

    class _Audit:
        def emit(self, event, **fields):
            events.append((event, fields))

    cfg = WebhookConfig(
        default_urls=["http://hook"], default_secret="k",
        max_attempts=3, backoff_base_s=0.0, backoff_max_s=0.0,
    )
    disp = WebhookDispatcher(cfg, audit=_Audit())
    await disp.dispatch(event="approval.granted", record_public={"tenant_id": "acme"})
    assert len(rec.calls) == 3
    assert any(e[0] == "approval.webhook_failed" for e in events)


@pytest.mark.asyncio
async def test_no_secret_skips_delivery_and_audits(monkeypatch):
    rec = _Recorder([200])
    _patch_client(monkeypatch, rec)
    events = []

    class _Audit:
        def emit(self, event, **fields):
            events.append(event)

    cfg = WebhookConfig(tenant_urls={"acme": ["http://hook"]})  # no secret
    disp = WebhookDispatcher(cfg, audit=_Audit())
    await disp.dispatch(event="approval.granted", record_public={"tenant_id": "acme"})
    assert rec.calls == []  # never sent unsigned
    assert "approval.webhook_failed" in events


@pytest.mark.asyncio
async def test_no_url_for_tenant_is_noop(monkeypatch):
    rec = _Recorder([200])
    _patch_client(monkeypatch, rec)
    cfg = WebhookConfig(tenant_urls={"other": ["http://hook"]}, default_secret="k")
    disp = WebhookDispatcher(cfg)
    await disp.dispatch(event="approval.granted", record_public={"tenant_id": "acme"})
    assert rec.calls == []
