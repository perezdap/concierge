from concierge.util.sanitize import (
    make_canonical_name,
    sanitize_description,
    sanitize_label,
    sanitize_primitive_name,
    summarize_arguments,
    validate_input_schema,
)


def test_sanitize_description_strips_control_chars_and_caps_length():
    raw = "hello\x00world\x07 " + ("x" * 1000)
    s = sanitize_description(raw)
    assert "\x00" not in s
    assert "\x07" not in s
    assert len(s) <= 605  # MAX_DESC_LEN + ellipsis


def test_sanitize_description_none_returns_empty():
    assert sanitize_description(None) == ""


def test_sanitize_primitive_name_replaces_unsafe_chars():
    assert sanitize_primitive_name("search repos!") == "search_repos"
    assert sanitize_primitive_name("a/b/c") == "a_b_c"


def test_make_canonical_name_format():
    assert make_canonical_name("github", "search_repos") == "github.search_repos"


def test_validate_input_schema_drops_unknown_types():
    s = validate_input_schema({"type": "object", "properties": {"x": {"type": "weird"}}})
    assert s.get("properties", {}).get("x", {}).get("type") != "weird"


def test_summarize_arguments_lists_required():
    schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "description": "hi"},
            "n": {"type": "integer"},
        },
    }
    args = summarize_arguments(schema)
    by_name = {a["name"]: a for a in args}
    assert by_name["text"]["required"] is True
    assert by_name["n"]["required"] is False


def test_sanitize_label_safely_handles_non_string():
    assert sanitize_label(12345) == "12345"
