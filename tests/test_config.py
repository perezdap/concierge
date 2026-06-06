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


# --- Comment-aware substitution (follow-up to the Twelve-Factor PR) ---


def test_expand_env_skips_references_inside_yaml_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reference inside a `#` comment must be left alone AND must not raise
    # even though the variable is unset with no default.
    monkeypatch.delenv("SOMETHING_UNDEFINED", raising=False)
    text = "# example: set ${SOMETHING_UNDEFINED} for debugging\nkey: value\n"
    assert expand_env(text) == text


def test_expand_env_still_raises_for_undefined_in_value_after_comment_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: the comment-handling change must not weaken the
    # unset-without-default contract for genuine value references.
    monkeypatch.delenv("DEFINITELY_UNSET", raising=False)
    with pytest.raises(ValueError, match="DEFINITELY_UNSET"):
        expand_env("token: ${DEFINITELY_UNSET}")


def test_expand_env_substitutes_value_and_skips_comment_on_same_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `value  # comment with ${UNSET}` should expand the value, leave the
    # comment alone, and NOT raise for the commented-out reference.
    monkeypatch.setenv("SET_VAR", "expanded")
    monkeypatch.delenv("UNSET", raising=False)
    out = expand_env("key: ${SET_VAR}  # see ${UNSET} for context\n")
    assert out == "key: expanded  # see ${UNSET} for context\n"


def test_expand_env_expands_inside_quoted_string_with_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `#` inside a quoted scalar is NOT a YAML comment — the reference
    # inside it must still be expanded.
    monkeypatch.setenv("NOTES_TOKEN", "s3cr3t")
    out = expand_env('label: "value # not a comment ${NOTES_TOKEN}"\n')
    assert out == 'label: "value # not a comment s3cr3t"\n'


def test_expand_env_expands_inside_single_quoted_string_with_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("X", "ok")
    out = expand_env("label: 'value # still not a comment ${X}'\n")
    assert out == "label: 'value # still not a comment ok'\n"


def test_expand_env_preserves_newlines_and_indentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Round-trip stability: newlines, blank lines, and the comment that
    # contains a placeholder are all preserved byte-for-byte.
    monkeypatch.delenv("UNSET", raising=False)
    text = "a: 1\n\nb: 2   # ${UNSET}\n"
    assert expand_env(text) == text


def test_expand_env_collects_all_undefined_value_references_in_one_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Multi-line input: one ValueError that names every undefined value
    # reference, sorted and de-duplicated. Comments must not contribute.
    monkeypatch.delenv("A", raising=False)
    monkeypatch.delenv("B", raising=False)
    with pytest.raises(ValueError) as ei:
        expand_env(
            "# doc: ${A}\n"
            "x: ${A}\n"
            "y: ${B}\n"
            "z: ${A}\n"
        )
    msg = str(ei.value)
    assert "A" in msg
    assert "B" in msg
    # Sorted: A before B
    assert msg.index("A") < msg.index("B")
