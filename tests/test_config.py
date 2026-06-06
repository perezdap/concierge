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


# --- Set-but-empty warning (follow-up to the Twelve-Factor PR) ---


def test_expand_env_warns_when_set_to_empty_string(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Footgun: operator copies .env.example, leaves BM_LIVE_TOKEN= (empty).
    # We must emit a WARNING that names the variable — not raise, not stay
    # silent.
    monkeypatch.setenv("BM_LIVE_TOKEN", "")
    caplog.set_level("WARNING", logger="concierge.config")
    assert expand_env("Authorization: Bearer ${BM_LIVE_TOKEN}") == "Authorization: Bearer "
    assert any(
        "BM_LIVE_TOKEN" in rec.message and "empty string" in rec.message
        for rec in caplog.records
        if rec.levelname == "WARNING"
    ), [rec.message for rec in caplog.records]


def test_expand_env_no_warning_when_value_is_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The healthy path: a real value is substituted and NO warning fires.
    monkeypatch.setenv("BM_LIVE_TOKEN", "actual-token")
    caplog.set_level("WARNING", logger="concierge.config")
    assert expand_env("Authorization: Bearer ${BM_LIVE_TOKEN}") == "Authorization: Bearer actual-token"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_expand_env_no_warning_for_explicit_empty_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # ${VAR:-} is an explicit, intentional empty value. The operator
    # asked for it; we must not warn even if VAR is also set to "".
    monkeypatch.setenv("MAYBE_EMPTY", "")
    caplog.set_level("WARNING", logger="concierge.config")
    assert expand_env("x: ${MAYBE_EMPTY:-}") == "x: "
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_expand_env_warns_once_per_variable_even_if_referenced_twice(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Multiple references to the same empty variable produce ONE warning,
    # not N. Tested by counting WARNING records mentioning the variable.
    monkeypatch.setenv("BM_LIVE_TOKEN", "")
    caplog.set_level("WARNING", logger="concierge.config")
    expand_env("a: ${BM_LIVE_TOKEN}\nb: ${BM_LIVE_TOKEN}\nc: ${BM_LIVE_TOKEN}\n")
    matches = [r for r in caplog.records if r.levelname == "WARNING" and "BM_LIVE_TOKEN" in r.message]
    assert len(matches) == 1


def test_expand_env_warns_for_multiple_distinct_empty_variables(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Two different empty variables → two distinct warnings, each naming
    # its own variable, sorted.
    monkeypatch.setenv("A", "")
    monkeypatch.setenv("B", "")
    caplog.set_level("WARNING", logger="concierge.config")
    expand_env("a: ${A}\nb: ${B}\n")
    msgs = sorted(r.message for r in caplog.records if r.levelname == "WARNING")
    assert len(msgs) == 2
    assert any("config references A" in m for m in msgs)
    assert any("config references B" in m for m in msgs)


def test_expand_env_warning_distinct_from_undefined_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Set-but-empty → warning, substitution proceeds. The error path for
    # unset variables is unchanged and must still raise.
    monkeypatch.setenv("EMPTY", "")
    monkeypatch.delenv("UNSET", raising=False)
    caplog.set_level("WARNING", logger="concierge.config")
    # Empty case: does not raise
    assert expand_env("a: ${EMPTY}") == "a: "
    # Unset case: still raises
    with pytest.raises(ValueError, match="UNSET"):
        expand_env("b: ${UNSET}")


def test_expand_env_does_not_warn_for_undefined_with_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # ${VAR:-default} where VAR is unset: no warning, no error, default used.
    monkeypatch.delenv("MAYBE_UNSET", raising=False)
    caplog.set_level("WARNING", logger="concierge.config")
    assert expand_env("a: ${MAYBE_UNSET:-fallback}") == "a: fallback"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
