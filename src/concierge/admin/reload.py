"""Hot-reload orchestration for admin-managed runtime config (P2-ADMIN-2)."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import GatewayConfig
from ..util.audit import AuditLogger

# Audit event names consumed by admin config APIs and operators.
EVENT_CONFIG_VALIDATE = "admin.config.validate"
EVENT_CONFIG_APPLY = "admin.config.apply"
EVENT_CONFIG_RELOAD = "admin.config.reload"
EVENT_CONFIG_ROLLBACK = "admin.config.rollback"


class ReloadError(Exception):
    """Reload pipeline failure with a stable code for API mapping."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


# GatewayService attributes that are wired once at build_app time and must NOT
# be overwritten during a hot-reload swap.  Everything else is swappable.
_SERVICE_STABLE_ATTRS: frozenset[str] = frozenset({"sessions", "audit", "bus", "primitives"})


@dataclass
class RuntimeBundle:
    """Swappable gateway runtime handles built from one GatewayConfig."""

    config: GatewayConfig
    catalog: Any
    adapters: Any
    publishing: Any
    profiles: Any
    policy: Any
    service: Any
    output_filter: Any | None = None
    rate_limiter: Any | None = None
    approval_store: Any | None = None

    async def start(self) -> None:
        await self.adapters.start_all()

    async def stop(self) -> None:
        await self.adapters.stop_all()

    async def refresh_catalog(self) -> None:
        refresh = getattr(self.adapters, "refresh_all", None)
        if callable(refresh):
            await refresh()
            return
        for adapter in self.adapters.all():
            await self.adapters.refresh_server(adapter.server_id)

    def swap_into(self, state: Any) -> None:
        """Atomically update live app.state handles from this bundle.

        Catalog: the existing Catalog object's store is replaced so callers
        holding a reference to the old Catalog continue to work.
        AdapterManager / ProfileRegistry: same in-place replacement of the
        internal dict so existing references see the new state.
        GatewayService: every instance attribute from the new service is
        applied except the stable-at-build-time attrs (sessions, audit, bus,
        primitives) which must not change across reloads.
        """
        # ── Catalog ──────────────────────────────────────────────────────────
        catalog = getattr(state, "catalog", None)
        if catalog is not None:
            catalog.store = self.catalog.store
        else:
            catalog = self.catalog

        # ── AdapterManager ───────────────────────────────────────────────────
        adapters = getattr(state, "adapters", None)
        if adapters is not None:
            adapters.__dict__.clear()
            adapters.__dict__.update(self.adapters.__dict__)
        else:
            adapters = self.adapters

        # ── ProfileRegistry ──────────────────────────────────────────────────
        profiles = getattr(state, "profiles", None)
        if profiles is not None:
            profiles._profiles = self.profiles._profiles  # type: ignore[attr-defined]
        else:
            profiles = self.profiles

        # ── GatewayService ───────────────────────────────────────────────────
        # Drive from the new service's own __dict__ rather than a hardcoded
        # list, so a new attribute added to GatewayService is swapped
        # automatically and can't silently fall through.
        service = getattr(state, "service", None)
        if service is not None:
            for attr, val in vars(self.service).items():
                if attr in _SERVICE_STABLE_ATTRS:
                    continue
                setattr(service, attr, val)
            # Point the swapped service at the in-place catalog/adapters/profiles
            # so all three references stay consistent.
            service.catalog = catalog
            service.adapters = adapters
            service.profiles = profiles
        else:
            service = self.service

        # ── app.state ────────────────────────────────────────────────────────
        state.catalog = catalog
        state.adapters = adapters
        state.publishing = self.publishing
        state.profiles = profiles
        state.service = service
        state.rate_limiter = self.rate_limiter
        state.approval_store = self.approval_store


RuntimeBuilder = Callable[[GatewayConfig], Awaitable[RuntimeBundle]]


class RuntimeSwapTarget(Protocol):
    """Mutable holder (e.g. app.state) that ReloadCoordinator updates on apply.

    Implementations must call ``bundle.swap_into(state)`` or perform an
    equivalent in-place update.  The :class:`RuntimeBundle.swap_into` helper is
    the canonical implementation; custom subclasses are for tests only.
    """

    def apply_runtime(self, bundle: RuntimeBundle) -> None: ...


@dataclass
class ValidationResult:
    ok: bool
    version_id: str
    error: str | None = None
    bundle: RuntimeBundle | None = None


@dataclass
class ApplyResult:
    ok: bool
    version_id: str
    error: str | None = None
    rolled_back: bool = False


@dataclass
class ReloadCoordinator:
    """Validate, apply, reload, and rollback gateway runtime without process restart."""

    audit: AuditLogger
    build_runtime: RuntimeBuilder
    swap_target: RuntimeSwapTarget | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _active: RuntimeBundle | None = field(default=None, init=False)
    _last_known_good: RuntimeBundle | None = field(default=None, init=False)

    @property
    def active(self) -> RuntimeBundle | None:
        return self._active

    @property
    def last_known_good(self) -> RuntimeBundle | None:
        return self._last_known_good

    def _emit(
        self,
        event: str,
        *,
        version_id: str,
        ok: bool,
        subject: str = "",
        **fields: Any,
    ) -> None:
        self.audit.emit(
            event,
            version_id=version_id,
            ok=ok,
            subject=subject,
            **fields,
        )

    async def validate(
        self,
        config: GatewayConfig,
        *,
        version_id: str,
        subject: str = "",
        retain_candidate: bool = False,
    ) -> ValidationResult:
        """Build candidate runtime and verify upstream connectivity.

        By default (validate-only) the candidate is torn down before returning so
        adapter sessions/tasks are not leaked. Set ``retain_candidate=True`` only
        when the caller will immediately promote via :meth:`apply`.
        """
        bundle: RuntimeBundle | None = None
        try:
            bundle = await self.build_runtime(config)
            await bundle.start()
            self._emit(
                EVENT_CONFIG_VALIDATE,
                version_id=version_id,
                ok=True,
                subject=subject,
            )
            if retain_candidate:
                return ValidationResult(ok=True, version_id=version_id, bundle=bundle)
            await bundle.stop()
            return ValidationResult(ok=True, version_id=version_id, bundle=None)
        except Exception as e:  # noqa: BLE001
            self._emit(
                EVENT_CONFIG_VALIDATE,
                version_id=version_id,
                ok=False,
                subject=subject,
                error=str(e),
            )
            if bundle is not None:
                try:
                    await bundle.stop()
                except Exception:  # noqa: BLE001
                    pass
            return ValidationResult(ok=False, version_id=version_id, error=str(e))

    async def validate_for_apply(
        self,
        config: GatewayConfig,
        *,
        version_id: str,
        subject: str = "",
    ) -> ValidationResult:
        """Validate and return a running candidate bundle for :meth:`apply`."""
        return await self.validate(
            config,
            version_id=version_id,
            subject=subject,
            retain_candidate=True,
        )

    async def apply(
        self,
        bundle: RuntimeBundle,
        *,
        version_id: str,
        subject: str = "",
    ) -> ApplyResult:
        """Promote a validated bundle to active runtime (atomic under lock)."""
        async with self._lock:
            previous = self._active
            swapped = False
            try:
                if self.swap_target is not None:
                    self.swap_target.apply_runtime(bundle)
                    swapped = True
                self._active = bundle
                if previous is not None:
                    self._last_known_good = previous
                    await previous.stop()
                elif self._last_known_good is None:
                    self._last_known_good = bundle
                self._emit(
                    EVENT_CONFIG_APPLY,
                    version_id=version_id,
                    ok=True,
                    subject=subject,
                )
                return ApplyResult(ok=True, version_id=version_id)
            except Exception as e:  # noqa: BLE001
                self._active = previous
                if self.swap_target is not None and previous is not None and swapped:
                    try:
                        self.swap_target.apply_runtime(previous)
                    except Exception:  # noqa: BLE001
                        pass
                self._emit(
                    EVENT_CONFIG_APPLY,
                    version_id=version_id,
                    ok=False,
                    subject=subject,
                    error=str(e),
                )
                return ApplyResult(ok=False, version_id=version_id, error=str(e))

    async def reload(
        self,
        *,
        version_id: str,
        subject: str = "",
    ) -> ApplyResult:
        """Refresh catalog/adapters from the active config (post-apply)."""
        if self._active is None:
            err = "no active runtime to reload"
            self._emit(
                EVENT_CONFIG_RELOAD,
                version_id=version_id,
                ok=False,
                subject=subject,
                error=err,
            )
            return ApplyResult(ok=False, version_id=version_id, error=err)
        try:
            await self._active.refresh_catalog()
            self._emit(
                EVENT_CONFIG_RELOAD,
                version_id=version_id,
                ok=True,
                subject=subject,
            )
            return ApplyResult(ok=True, version_id=version_id)
        except Exception as e:  # noqa: BLE001
            self._emit(
                EVENT_CONFIG_RELOAD,
                version_id=version_id,
                ok=False,
                subject=subject,
                error=str(e),
            )
            return ApplyResult(ok=False, version_id=version_id, error=str(e))

    async def rollback(
        self,
        *,
        version_id: str,
        subject: str = "",
    ) -> ApplyResult:
        """Restore the last-known-good runtime snapshot."""
        async with self._lock:
            lkg = self._last_known_good
            if lkg is None or lkg is self._active:
                err = "no prior runtime to roll back to"
                self._emit(
                    EVENT_CONFIG_ROLLBACK,
                    version_id=version_id,
                    ok=False,
                    subject=subject,
                    error=err,
                )
                return ApplyResult(ok=False, version_id=version_id, error=err)
            failed = self._active
            try:
                await lkg.start()
                if self.swap_target is not None:
                    self.swap_target.apply_runtime(lkg)
                self._active = lkg
                if failed is not None and failed is not lkg:
                    await failed.stop()
                self._emit(
                    EVENT_CONFIG_ROLLBACK,
                    version_id=version_id,
                    ok=True,
                    subject=subject,
                )
                return ApplyResult(ok=True, version_id=version_id, rolled_back=True)
            except Exception as e:  # noqa: BLE001
                self._emit(
                    EVENT_CONFIG_ROLLBACK,
                    version_id=version_id,
                    ok=False,
                    subject=subject,
                    error=str(e),
                )
                return ApplyResult(
                    ok=False,
                    version_id=version_id,
                    error=str(e),
                    rolled_back=False,
                )

    async def validate_apply_reload(
        self,
        config: GatewayConfig,
        *,
        version_id: str,
        subject: str = "",
    ) -> ApplyResult:
        """Full pipeline used by admin promote+reload: validate → apply → reload."""
        validation = await self.validate_for_apply(
            config,
            version_id=version_id,
            subject=subject,
        )
        if not validation.ok or validation.bundle is None:
            raise ReloadError("validate_failed", validation.error or "validation failed")
        bundle = validation.bundle
        applied = await self.apply(
            bundle,
            version_id=version_id,
            subject=subject,
        )
        if not applied.ok:
            await bundle.stop()
            raise ReloadError("apply_failed", applied.error or "apply failed")
        reloaded = await self.reload(version_id=version_id, subject=subject)
        if not reloaded.ok:
            rb = await self.rollback(version_id=version_id, subject=subject)
            if not rb.ok:
                raise ReloadError("reload_failed", reloaded.error or "reload failed")
            raise ReloadError("reload_failed_rolled_back", reloaded.error or "reload failed")
        return reloaded