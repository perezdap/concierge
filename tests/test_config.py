"""Configuration defaults and validation."""

import pytest
from pydantic import ValidationError

from concierge.config import GatewayConfig, PayloadConfig, ProfileConfig, SessionPoolConfig


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
