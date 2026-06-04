"""Approval-decision webhooks (P1-3).

On every grant / deny the gateway can POST a signed JSON payload to one or more
operator-configured URLs (per-tenant, or a global default). The payload is signed
with HMAC-SHA256 over the *exact* request body bytes using a per-tenant signing
secret, and the signature is sent in an ``X-Concierge-Signature: sha256=<hex>``
header — the same scheme GitHub / Stripe use, so existing receiver libraries work.

Delivery is best-effort with bounded exponential backoff: a non-2xx response (or
a transport error) is retried up to ``max_attempts`` times with growing delay;
once the cap is hit we give up and record the failure in the audit log. Webhook
delivery never blocks or fails the approval decision itself.

Hygiene: the signing secret is never logged or echoed; only the opaque approval
id, tenant, and tool name appear in audit/diagnostic records.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field

import httpx

from ..util.log import get_logger

_log = get_logger("concierge.approval.webhook")

SIGNATURE_HEADER = "X-Concierge-Signature"


def sign_payload(secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` signature header value for ``body``."""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def verify_signature(secret: str, body: bytes, header_value: str) -> bool:
    """Constant-time verification of an ``X-Concierge-Signature`` header.

    Receivers should call this with the *raw* request body bytes. Uses
    ``hmac.compare_digest`` so verification time does not leak how much of the
    signature matched.
    """
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, header_value or "")


@dataclass
class WebhookConfig:
    """Resolved webhook delivery settings.

    ``tenant_urls`` maps ``tenant_id -> [url, ...]``; ``default_urls`` apply to
    any tenant without an explicit entry. ``tenant_secrets`` / ``default_secret``
    supply the HMAC signing key (per-tenant secret preferred, else the default).
    """

    tenant_urls: dict[str, list[str]] = field(default_factory=dict)
    default_urls: list[str] = field(default_factory=list)
    tenant_secrets: dict[str, str] = field(default_factory=dict)
    default_secret: str | None = None
    max_attempts: int = 4
    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0
    timeout_s: float = 5.0

    def urls_for(self, tenant_id: str) -> list[str]:
        return self.tenant_urls.get(tenant_id) or self.default_urls

    def secret_for(self, tenant_id: str) -> str | None:
        return self.tenant_secrets.get(tenant_id) or self.default_secret

    def enabled_for(self, tenant_id: str) -> bool:
        return bool(self.urls_for(tenant_id))


class WebhookDispatcher:
    """Sends signed approval-decision callbacks with bounded retry/backoff."""

    def __init__(self, cfg: WebhookConfig, *, audit: object | None = None) -> None:
        self._cfg = cfg
        self._audit = audit

    async def dispatch(self, *, event: str, record_public: dict[str, object]) -> None:
        """Deliver one decision event to every URL configured for the tenant.

        Returns once delivery has been attempted (and retried) for each URL. A
        terminal failure is logged + audited but never raised — the decision has
        already been recorded authoritatively in the store.
        """
        tenant_id = str(record_public.get("tenant_id", "default"))
        urls = self._cfg.urls_for(tenant_id)
        if not urls:
            return
        secret = self._cfg.secret_for(tenant_id)
        if not secret:
            # Refuse to send an unsigned callback — a receiver could not trust it.
            self._audit_fail(event, record_public, reason="no_signing_secret", attempts=0)
            return

        payload = {"event": event, "approval": record_public}
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        signature = sign_payload(secret, body)
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: signature,
            "X-Concierge-Event": event,
        }
        for url in urls:
            await self._deliver_one(url, body, headers, event, record_public)

    async def _deliver_one(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        event: str,
        record_public: dict[str, object],
    ) -> None:
        last_status: int | None = None
        async with httpx.AsyncClient(timeout=self._cfg.timeout_s) as client:
            for attempt in range(1, self._cfg.max_attempts + 1):
                try:
                    resp = await client.post(url, content=body, headers=headers)
                    last_status = resp.status_code
                    if 200 <= resp.status_code < 300:
                        return
                except httpx.HTTPError as e:
                    last_status = None
                    _log.warning("approval webhook attempt %d failed: %s", attempt, e)
                if attempt < self._cfg.max_attempts:
                    delay = min(
                        self._cfg.backoff_max_s,
                        self._cfg.backoff_base_s * (2 ** (attempt - 1)),
                    )
                    await asyncio.sleep(delay)
        self._audit_fail(
            event, record_public, reason="max_attempts_exhausted",
            attempts=self._cfg.max_attempts, last_status=last_status,
        )

    def _audit_fail(
        self,
        event: str,
        record_public: dict[str, object],
        *,
        reason: str,
        attempts: int,
        last_status: int | None = None,
    ) -> None:
        emit = getattr(self._audit, "emit", None)
        if emit is None:
            return
        emit(
            "approval.webhook_failed",
            decision_event=event,
            approval_id=record_public.get("approval_id"),
            tenant_id=record_public.get("tenant_id"),
            tool=record_public.get("tool"),
            reason=reason,
            attempts=attempts,
            last_status=last_status,
        )


__all__ = [
    "SIGNATURE_HEADER",
    "WebhookConfig",
    "WebhookDispatcher",
    "sign_payload",
    "verify_signature",
]
