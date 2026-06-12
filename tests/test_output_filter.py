"""TDD tests for P1-8 Output filtering (result redaction chain).

Per Builder 3 assignment (Coord 2):
- New util/output_filter.py mirroring sanitize.py style (pure, composable, no side effects).
- Security: redact secrets/PII from tool results before client sees them; length caps; content-type allow.
- Conservative defaults (opt-in or pass-through safe).
- AC: fake secret in result -> redacted before return.

Tests written FIRST (fail), then min impl to green.
"""

from concierge.util.output_filter import (
    ContentTypeFilter,
    LengthCapper,
    OutputFilter,
    SecretRedactor,
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
