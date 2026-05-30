"""Configuration defaults and validation."""

import pytest
from pydantic import ValidationError

from concierge.config import (
    GatewayConfig,
    PayloadConfig,
    ProfileConfig,
    SessionPoolConfig,
    expand_env,
)


def test_payload_slim_tools_list_is_opt_in_by_default():
    assert PayloadConfig().slim_tools_list is False
    assert GatewayConfig().payload.slim_tools_list is False


def test_profile_auto_apply_is_opt_in_by_default():
    assert ProfileConfig(name="p").auto_apply is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("idle_ttl_s", 0),
        ("gc_interval_s", 0),
        ("max_upstream_sessions", 0),
    ],
)
def test_session_pool_rejects_non_positive_values(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        SessionPoolConfig.model_validate({field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_schema_description_chars", -1),
        ("max_result_bytes", -1),
    ],
)
def test_payload_rejects_negative_limits(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        PayloadConfig.model_validate({field: value})


def test_expand_env_substitutes_set_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTES_TOKEN", "s3cr3t")
    assert expand_env("Bearer ${NOTES_TOKEN}") == "Bearer s3cr3t"


def test_expand_env_uses_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAYBE_UNSET", raising=False)
    assert expand_env("${MAYBE_UNSET:-fallback}") == "fallback"


def test_expand_env_raises_on_undefined_without_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEFINITELY_UNSET", raising=False)
    with pytest.raises(ValueError, match="DEFINITELY_UNSET"):
        expand_env("token: ${DEFINITELY_UNSET}")
