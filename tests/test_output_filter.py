"""TDD tests for P1-8 Output filtering (result redaction chain).

Per Builder 3 assignment (Coord 2):
- New util/output_filter.py mirroring sanitize.py style (pure, composable, no side effects).
- Security: redact secrets/PII from tool results before client sees them; length caps; content-type allow.
- Conservative defaults (opt-in or pass-through safe).
- AC: fake secret in result -> redacted before return.

Tests written FIRST (fail), then min impl to green.
"""

import pytest

from concierge.util.output_filter import (
    ContentTypeFilter,
    LengthCapper,
    OutputFilter,
    SecretRedactor,
    _redact_text,
)


def test_output_filter_redacts_fake_secret():
    """Core AC: a result containing a fake secret (e.g. token-like) must be redacted."""
    raw = {
        "content": [
            {"type": "text", "text": "Here is my secret: sk-1234567890abcdef for the API."}
        ],
        "isError": False,
    }
    f = OutputFilter([SecretRedactor()])
    cleaned = f.apply(raw)
    text = cleaned["content"][0]["text"]
    assert "sk-1234567890abcdef" not in text
    assert "[REDACTED_SECRET]" in text or "[REDACTED]" in text or "[REDACTED_LONG_TOKEN]" in text  # either marker acceptable for this pattern set


def test_output_filter_caps_long_text():
    """Length capper prevents huge results (DoS / token bloat)."""
    long_text = "x" * 10000
    raw = {"content": [{"type": "text", "text": long_text}]}
    f = OutputFilter([LengthCapper(max_bytes=100)])
    cleaned = f.apply(raw)
    assert len(cleaned["content"][0]["text"]) <= 120  # cap + truncation marker (len tolerant for impl)
    assert cleaned["content"][0]["_meta"]["gateway_truncated"] is True


def test_output_filter_chains_multiple():
    """Filters compose (DRY, security in depth)."""
    raw = {"content": [{"type": "text", "text": "secret: sk-abc123 long:" + "y"*500}]}
    f = OutputFilter([SecretRedactor(), LengthCapper(max_bytes=50)])
    cleaned = f.apply(raw)
    t = cleaned["content"][0]["text"]
    assert "sk-abc123" not in t or "[REDACTED" in t  # redaction occurred (marker may vary by pattern match order)
    assert len(t) <= 80


def test_content_type_filter_allows_text_by_default():
    """Default allow-list includes text."""
    raw = {"content": [{"type": "text", "text": "hello"}]}
    f = ContentTypeFilter()
    assert f.apply(raw) == raw


def test_content_type_filter_blocks_text_when_not_allowed():
    """Operator allow-list is respected: text can be filtered out."""
    raw = {"content": [{"type": "text", "text": "should be filtered"}]}
    f = ContentTypeFilter(allowed={"json"})
    cleaned = f.apply(raw)
    assert cleaned["content"][0] == {
        "type": "text",
        "text": "[filtered content-type: text]",
    }


def test_output_filter_pass_through_when_no_rules():
    """Default safe: empty filter or disabled returns original (conservative)."""
    raw = {"content": [{"type": "text", "text": "normal result"}]}
    f = OutputFilter([])
    assert f.apply(raw) == raw


def test_content_type_filter_replaces_disallowed_type():
    """Non-text, non-allowed content types are replaced with a placeholder."""
    raw = {"content": [{"type": "image/png", "data": "base64data"}]}
    f = OutputFilter([ContentTypeFilter()])
    cleaned = f.apply(raw)
    assert cleaned["content"] == [
        {"type": "text", "text": "[filtered content-type: image/png]"}
    ]


def test_length_capper_disabled_with_non_positive_max():
    """A non-positive cap is a no-op pass-through."""
    long_text = "x" * 10000
    raw = {"content": [{"type": "text", "text": long_text}]}
    capper = LengthCapper(max_bytes=0)
    assert capper.apply(raw)["content"][0]["text"] == long_text


def test_secret_redactor_passes_through_non_dict():
    """Non-dict inputs are returned unchanged."""
    redactor = SecretRedactor()
    assert redactor.apply("not a dict") == "not a dict"


def test_redact_text_passes_through_non_string():
    """_redact_text returns non-string inputs unchanged."""
    assert _redact_text(123) == 123


def test_secret_redactor_preserves_long_identifiers_and_urls():
    """Generic long-token catch-all must not mangle ordinary tool output (issue #6)."""
    raw = {
        "content": [
            {
                "type": "text",
                "text": (
                    "See https://example.com/infrastructure-as-code "
                    "and id 550e8400-e29b-41d4-a716-446655440000"
                ),
            }
        ]
    }
    cleaned = SecretRedactor().apply(raw)
    assert cleaned["content"][0]["text"] == raw["content"][0]["text"]
    assert "[REDACTED_LONG_TOKEN]" not in cleaned["content"][0]["text"]


def test_content_type_filter_respects_allow_list_for_text():
    """Operator allow-list without 'text' must filter text items (no bypass)."""
    f = ContentTypeFilter(allowed={"json"})
    raw = {"content": [{"type": "text", "text": "hello"}, {"type": "json", "text": "{}"}]}
    result = f.apply(raw)
    assert result["content"][0]["text"] == "[filtered content-type: text]"
    assert result["content"][1]["type"] == "json"


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAIOSFODNN7EXAMPLE",  # AWS access key id
        "ASIAIOSFODNN7EXAMPLE",  # AWS temporary access key id
        "github_pat_11ABCDE0Y0abcdefghij_klmnopqrstuvwxyz0123456789ABCDEF",  # GitHub fine-grained PAT
        "gho_16C7e42F292c6912E7710c838347Ae178B4a",  # GitHub OAuth token
        "xoxb-1234567890-abcdefghijklmnop",  # Slack bot token
        "AIzaSyA1234567890abcdefghijklmnopqrstuvw",  # Google API key
    ],
)
def test_secret_redactor_catches_common_prefixed_credentials(secret: str) -> None:
    """Distinctly-prefixed credential formats must still be redacted (issue #6 follow-up)."""
    raw = {"content": [{"type": "text", "text": f"token={secret} done"}]}
    cleaned = SecretRedactor().apply(raw)
    text = cleaned["content"][0]["text"]
    assert secret not in text
    assert "[REDACTED_SECRET]" in text
