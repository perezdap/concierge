"""
Payload optimization helpers.

Reduces JSON/token bloat between the gateway and downstream LLM clients:

* ``slim_schema`` — strip verbose, model-irrelevant keys (examples, $comment)
  and cap long inline descriptions from a JSON Schema, while preserving the
  structure a model needs to *call* the tool (type / properties / required).
* ``cap_result_text`` — opt-in length cap for heavy text results, with an
  explicit truncation marker so truncation is never silent.

These are framework-agnostic and operate on plain dicts so they are trivially
testable and reusable from the admin (rich) vs model (slim) surfaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Keys that are useful to humans/tooling but cost tokens without helping a model
# decide how to *call* a tool. Dropped from slimmed schemas.
_VERBOSE_SCHEMA_KEYS = ("examples", "example", "$comment", "$schema")


@dataclass
class PayloadOptions:
    """Knobs controlling how the gateway slims outbound payloads."""
    slim_tools_list: bool = True
    max_schema_description_chars: int = 160
    drop_schema_examples: bool = True
    # 0 disables the result text cap (default — never truncate by surprise).
    max_result_bytes: int = 0


def slim_schema(
    schema: Any,
    *,
    max_desc: int = 160,
    drop_examples: bool = True,
) -> Any:
    """Return a slimmed copy of a JSON Schema. Non-dict inputs pass through."""
    if not isinstance(schema, dict):
        return schema
    return _slim(schema, max_desc, drop_examples)


def _slim(node: Any, max_desc: int, drop_examples: bool) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if drop_examples and k in _VERBOSE_SCHEMA_KEYS:
                continue
            if k == "description" and isinstance(v, str) and max_desc > 0 and len(v) > max_desc:
                out[k] = v[:max_desc].rstrip() + "…"
                continue
            out[k] = _slim(v, max_desc, drop_examples)
        return out
    if isinstance(node, list):
        return [_slim(x, max_desc, drop_examples) for x in node]
    return node


def cap_result_text(result: Any, max_bytes: int) -> Any:
    """Cap oversized ``content[].text`` blocks in a tool result.

    No-op when ``max_bytes <= 0`` or the result has no text content. When a block
    is truncated, a marker is appended and ``_meta.gateway_truncated`` is set so
    downstream consumers can detect it. Structured content is never altered.
    """
    if max_bytes <= 0 or not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        return result

    truncated = False
    new_content: list[Any] = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            encoded = block["text"].encode("utf-8")
            if len(encoded) > max_bytes:
                clipped = encoded[:max_bytes].decode("utf-8", "ignore")
                block = {**block, "text": clipped + "\n…[truncated by gateway]"}
                truncated = True
        new_content.append(block)

    if not truncated:
        return result
    out = {**result, "content": new_content}
    meta = dict(out.get("_meta") or {})
    meta["gateway_truncated"] = True
    out["_meta"] = meta
    return out
