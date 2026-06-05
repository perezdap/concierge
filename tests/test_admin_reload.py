"""Tests for admin hot-reload orchestration (P2-ADMIN-2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from concierge.admin.reload import (
    EVENT_CONFIG_APPLY,
    EVENT_CONFIG_ROLLBACK,
    EVENT_CONFIG_VALIDATE,
    ReloadCoordinator,
    ReloadError,
    RuntimeBundle,
)
from concierge.config import GatewayConfig
from concierge.util.audit import AuditLogger


@dataclass
class FakeAdapter:
    server_id: str = "fake"


@dataclass
class FakeAdapters:
    start_calls: int = 0
    stop_calls: int = 0
    refresh_calls: int = 0
    fail_connect: bool = False
    fail_refresh: bool = False

    async def start_all(self) -> None:
        self.start_calls += 1
        if self.fail_connect:
            raise ConnectionError("upstream connect failed")

    async def stop_all(self) -> None:
        self.stop_calls += 1

    def all(self) -> list[FakeAdapter]:
        return [FakeAdapter()]

    async def refresh_server(self, _server_id: str) -> int:
        self.refresh_calls += 1
        if self.fail_refresh:
            raise RuntimeError("refresh failed")
        return 0


@dataclass
class FakeSwapTarget:
    bundles: list[RuntimeBundle] = field(default_factory=list)

    def apply_runtime(self, bundle: RuntimeBundle) -> None:
        self.bundles.append(bundle)


def _bundle(
    *,
    label: str,
    adapters: FakeAdapters | None = None,
) -> RuntimeBundle:
    cfg = GatewayConfig()
    adapters = adapters or FakeAdapters()
    return RuntimeBundle(
        config=cfg,
        catalog=object(),
        adapters=adapters,
        publishing=object(),
        profiles=object(),
        policy=object(),
        service=object(),
    )


def _capture_audit() -> tuple[AuditLogger, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    class _Sink:
        def emit(self, event: str, payload: dict[str, Any]) -> None:
            events.append(payload)

    return AuditLogger(sinks=[_Sink()]), events


async def test_validate_only_disposes_candidate() -> None:
    audit, _ = _capture_audit()
    adapters = FakeAdapters()

    async def build(_cfg: GatewayConfig) -> RuntimeBundle:
        return _bundle(label="v2", adapters=adapters)

    coord = ReloadCoordinator(audit=audit, build_runtime=build)
    result = await coord.validate(GatewayConfig(), version_id="ver-1")
    assert result.ok
    assert result.bundle is None
    assert adapters.start_calls == 1
    assert adapters.stop_calls == 1


async def test_validate_only_leaves_active_runtime_unchanged() -> None:
    audit, _ = _capture_audit()
    active_adapters = FakeAdapters()
    candidate_adapters = FakeAdapters()

    async def build(_cfg: GatewayConfig) -> RuntimeBundle:
        return _bundle(label="candidate", adapters=candidate_adapters)

    coord = ReloadCoordinator(audit=audit, build_runtime=build)
    active = _bundle(label="active", adapters=active_adapters)
    coord._active = active  # noqa: SLF001

    result = await coord.validate(GatewayConfig(), version_id="ver-only")
    assert result.ok
    assert coord.active is active
    assert candidate_adapters.stop_calls == 1
    assert active_adapters.stop_calls == 0


async def test_validate_for_apply_retains_candidate() -> None:
    audit, _ = _capture_audit()
    adapters = FakeAdapters()

    async def build(_cfg: GatewayConfig) -> RuntimeBundle:
        return _bundle(label="v2", adapters=adapters)

    coord = ReloadCoordinator(audit=audit, build_runtime=build)
    result = await coord.validate_for_apply(GatewayConfig(), version_id="ver-apply")
    assert result.ok
    assert result.bundle is not None
    assert adapters.stop_calls == 0


async def test_validate_failure_records_audit() -> None:
    audit, events = _capture_audit()
    bad = FakeAdapters(fail_connect=True)

    async def build(_cfg: GatewayConfig) -> RuntimeBundle:
        return _bundle(label="bad", adapters=bad)

    coord = ReloadCoordinator(audit=audit, build_runtime=build)
    result = await coord.validate(GatewayConfig(), version_id="ver-bad")
    assert not result.ok
    assert any(e["event"] == EVENT_CONFIG_VALIDATE and e["ok"] is False for e in events)


async def test_apply_swaps_runtime_and_stops_previous() -> None:
    audit, _ = _capture_audit()
    swap = FakeSwapTarget()
    old_adapters = FakeAdapters()
    new_adapters = FakeAdapters()

    async def build(_cfg: GatewayConfig) -> RuntimeBundle:
        return _bundle(label="new", adapters=new_adapters)

    coord = ReloadCoordinator(audit=audit, build_runtime=build, swap_target=swap)
    old = _bundle(label="old", adapters=old_adapters)
    coord._active = old  # noqa: SLF001
    coord._last_known_good = old  # noqa: SLF001

    new = _bundle(label="new", adapters=new_adapters)
    applied = await coord.apply(new, version_id="ver-2")
    assert applied.ok
    assert coord.active is new
    assert old_adapters.stop_calls == 1
    assert swap.bundles[-1] is new


async def test_apply_failure_leaves_previous_active() -> None:
    audit, _ = _capture_audit()

    class FailingSwap(FakeSwapTarget):
        def apply_runtime(self, bundle: RuntimeBundle) -> None:
            if len(self.bundles) == 0:
                raise RuntimeError("swap failed")
            super().apply_runtime(bundle)

    swap = FailingSwap()
    old = _bundle(label="old")
    new = _bundle(label="new")

    async def build(_cfg: GatewayConfig) -> RuntimeBundle:
        return new

    coord = ReloadCoordinator(audit=audit, build_runtime=build, swap_target=swap)
    coord._active = old  # noqa: SLF001

    applied = await coord.apply(new, version_id="ver-fail")
    assert not applied.ok
    assert coord.active is old


async def test_reload_failure_triggers_rollback() -> None:
    audit, events = _capture_audit()
    swap = FakeSwapTarget()
    lkg_adapters = FakeAdapters()
    bad_adapters = FakeAdapters(fail_refresh=True)

    lkg = _bundle(label="lkg", adapters=lkg_adapters)
    bad = _bundle(label="bad", adapters=bad_adapters)

    async def build(_cfg: GatewayConfig) -> RuntimeBundle:
        return bad

    coord = ReloadCoordinator(audit=audit, build_runtime=build, swap_target=swap)
    coord._active = lkg  # noqa: SLF001
    coord._last_known_good = lkg  # noqa: SLF001
    await coord.apply(bad, version_id="ver-3")
    reloaded = await coord.reload(version_id="ver-3")
    assert not reloaded.ok

    rb = await coord.rollback(version_id="ver-3")
    assert rb.ok
    assert coord.active is lkg
    assert any(e["event"] == EVENT_CONFIG_ROLLBACK and e["ok"] is True for e in events)


async def test_validate_apply_reload_happy_path() -> None:
    audit, events = _capture_audit()
    swap = FakeSwapTarget()
    adapters = FakeAdapters()

    async def build(_cfg: GatewayConfig) -> RuntimeBundle:
        return _bundle(label="v-next", adapters=adapters)

    coord = ReloadCoordinator(audit=audit, build_runtime=build, swap_target=swap)
    result = await coord.validate_apply_reload(
        GatewayConfig(),
        version_id="ver-full",
        subject="operator:test",
    )
    assert result.ok
    assert adapters.start_calls >= 1
    assert adapters.refresh_calls == 1
    assert any(e["event"] == EVENT_CONFIG_APPLY for e in events)


async def test_validate_apply_reload_raises_on_connect_failure() -> None:
    audit, _ = _capture_audit()
    bad = FakeAdapters(fail_connect=True)

    async def build(_cfg: GatewayConfig) -> RuntimeBundle:
        return _bundle(label="bad", adapters=bad)

    coord = ReloadCoordinator(audit=audit, build_runtime=build)
    with pytest.raises(ReloadError, match="validate_failed"):
        await coord.validate_apply_reload(GatewayConfig(), version_id="ver-x")